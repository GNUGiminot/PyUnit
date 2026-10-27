"""Runtime control primitives for dryer hardware abstraction.

This module contains lightweight controller objects that wrap Moonraker API
interactions for the physical subsystems (servo, LED, heater, sensors) plus
PID helpers and the orchestrating `Dryer_control` class that coordinates
status evaluation, actuator decisions, and logging.

Design principles:
* Fire-and-forget soft servo movement to avoid blocking the main update loop.
* Idempotent update_status() methods – safe to call frequently with or without
    externally batched Moonraker query data.
* Separation of concerns: each subsystem controller is responsible only for
    its own API calls and state derivation; orchestration logic lives in
    `Dryer_control`.
"""

from sqlalchemy.ext.asyncio import AsyncSession
from typing import Optional, cast
from api.schemas import dryer_schema
from api.schemas import preset_schema
from api.logger import get_logger
from api.tools.moonraker_api import Moonraker_api
import asyncio
from api.database import get_db
from api.cruds.dryer_crud import dryer_crud
from simple_pid import PID
from collections import deque
import statistics
from datetime import datetime
import time
import json
from api.cruds.preset_crud import preset_crud


logger = get_logger("dryer")

# --- Проветривание (см. Dryer_control._vent_control) ---------------------------
# Решение принимается по АБСОЛЮТНОЙ влажности камеры (г/м³ — сколько воды в
# воздухе). Относительная влажность падает просто от нагрева и о том, сколько
# влаги отдала катушка, не говорит.
SERVO_SETTLE_S = 20        # после команды: заслонка доезжает (~16 с)
SERVO_MIN_DWELL_S = 60     # в каждом положении не меньше
SERVO_AH_RISE = 0.3        # г/м³: столько влаги накопилось в закрытой камере -> проветрить
SERVO_TREND_S = 120        # окно, по которому судим, что падение при проветривании кончилось
SERVO_AH_FALL = 0.05       # г/м³ в минуту: медленнее — проветривание больше не помогает
SERVO_MAX_OPEN_S = 600     # дольше не держим открытой (теряем тепло)
SERVO_BACKOFF_S = (300, 1800)  # пауза, если проветривание не помогло: от 5 до 30 мин

# --- Прогрев -> выдержка --------------------------------------------------------
# Цель по влажности бывает недостижима: при 50 °C комнатный воздух даёт
# 12-15 %, и пресет с целью 10 % прогревался бы до аварийного предела dry_time.
# Если воздух уже на температуре, а влажность перестала падать — выходим на
# выдержку.
DRY_PLATEAU_S = 1200       # столько воздух на температуре и влажность не улучшается
DRY_PLATEAU_RH = 0.5       # %: меньшее снижение влажности улучшением не считается
DRY_AT_TEMP_C = 2.0        # «на температуре» — не дальше этого от уставки
AIR_OVERHEAT_C = 2.0       # воздух выше потолка блока на столько — авария, нагрев выкл


def _ah_per_rh_step(temperature: float) -> float:
    """Сколько г/м³ даёт шаг датчика 0.1 % RH при этой температуре.
    При 60 °C ~0.13, при 85 °C ~0.35 — порог проветривания должен быть больше
    шума округления, иначе на горячих пресетах заслонка дёргается от него."""
    es = 6.112 * (2.71828 ** ((17.67 * temperature) / (temperature + 243.5)))
    return es * 0.1 * 2.1674 / (273.15 + temperature)

class Servo_control(object):
    """Servo control with fire-and-forget soft movement.

    Key Concepts:
    - desired_is_open: logical target (never None after first open/close)
    - physical_is_open: inferred from actual pulse width (may be None mid-travel)
    - _soft_task: background task performing stepped motion
    - DB should log desired_is_open to avoid None constraint violations
    """

    def __init__(self, moonraker_api: Moonraker_api, servo: dryer_schema.ServoConfig):
        self.servo = servo
        self.moonraker_api = moonraker_api
        self.close_pulse_width: Optional[float] = None
        self.open_pulse_width: Optional[float] = None
        self.current_pulse_width: Optional[float] = None
        self.desired_is_open: Optional[bool] = None
        self.physical_is_open: Optional[bool] = None
        self._soft_task: Optional[asyncio.Task] = None
        self._last_commanded_angle: Optional[int] = None
        # Defaults (overridden by servo config optional fields if present and >0)
        self.external_data: Optional[dict] = None
        logger.debug("Servo_control created name=%s", self.servo.name)

    async def _ensure_initialized(self):
        if self.close_pulse_width is None or self.open_pulse_width is None or self.current_pulse_width is None:
            logger.debug("Servo initializing pulse widths name=%s", self.servo.name)
            await self._get_close_and_open_pulse_width()
            # Force to closed baseline fast to align logical + physical
            await self.close(fast=True)

    async def close(self, fast: bool = False):
        await self._ensure_initialized()
        if self.desired_is_open == False and self.physical_is_open == False:
            return
        self.desired_is_open = False
        if fast:
            await self._set_angle(self.servo.close_angle)
        else:
            await self._soft_set_angle(self.servo.close_angle)

    async def open(self, fast: bool = False):
        await self._ensure_initialized()
        if self.desired_is_open == True and self.physical_is_open == True:
            return
        self.desired_is_open = True
        if fast:
            await self._set_angle(self.servo.open_angle)
        else:
            await self._soft_set_angle(self.servo.open_angle)

    async def update_status(self, external_data: dict = None):
        await self._ensure_initialized()
        self.external_data = external_data
        self.current_pulse_width = await self._get_status()

    async def _get_close_and_open_pulse_width(self):
        await self._set_angle(self.servo.open_angle)
        self.open_pulse_width = await self._get_status()
        await self._set_angle(self.servo.close_angle)
        self.close_pulse_width = await self._get_status()
        self.current_pulse_width = self.close_pulse_width
        logger.debug("Servo pulse calibration done name=%s open=%s close=%s", self.servo.name, self.open_pulse_width, self.close_pulse_width)

    async def _get_status(self):
        if self.external_data is None:
            url = f"{self.moonraker_api.url}/printer/objects/query?{self.servo.name}"
            result = await self.moonraker_api.call_api(url)
        else:
            result = self.external_data
            self.external_data = None
        return result['data']['result']['status'][self.servo.name]['value']

    async def _set_angle(self, angle: int):
        servo_name = " ".join(self.servo.name.split(" ")[1:])
        gcode = f"SET_SERVO SERVO={servo_name} ANGLE={angle}"
        result = await self.moonraker_api.send_gcode(gcode=gcode)
        if result['success']:
            self.current_pulse_width = await self._get_status()
            if self.current_pulse_width == self.open_pulse_width:
                self.physical_is_open = True
            elif self.current_pulse_width == self.close_pulse_width:
                self.physical_is_open = False
            else:
                self.physical_is_open = None
            self._last_commanded_angle = angle
            logger.debug(
                "Servo angle set name=%s angle=%s pulse=%s phys_is_open=%s desired_is_open=%s",
                self.servo.name, angle, self.current_pulse_width, self.physical_is_open, self.desired_is_open
            )
        else:
            logger.error("Servo angle set FAILED name=%s angle=%s response=%s", self.servo.name, angle, result)
            return result['success']

    async def _soft_set_angle(self, target_angle: int):
        """Fire-and-forget smooth move to target_angle.

        - Cancels previous soft task.
        - Leaves desired_is_open as previously set by open()/close().
        - physical_is_open updates only when pulse equals endpoints.
        - Guarantees final _set_angle(target_angle) call.
        """
        if self._soft_task and not self._soft_task.done():
            self._soft_task.cancel()
            logger.debug("Servo soft move canceled previous task name=%s new_target=%s", self.servo.name, target_angle)

        # Derive start angle
        if self.current_pulse_width == self.open_pulse_width:
            start_angle = self.servo.open_angle
        elif self.current_pulse_width == self.close_pulse_width:
            start_angle = self.servo.close_angle
        elif self._last_commanded_angle is not None:
            start_angle = self._last_commanded_angle
        else:
            # Choose nearest extreme
            if abs(target_angle - self.servo.open_angle) < abs(target_angle - self.servo.close_angle):
                start_angle = self.servo.open_angle
            else:
                start_angle = self.servo.close_angle

        step = self.servo.soft_step
        sleep = self.servo.soft_sleep
        logger.debug(
            "Servo soft move scheduled name=%s from=%s to=%s step=%s sleep=%.2f desired_is_open=%s",
            self.servo.name, start_angle, target_angle, step, sleep, self.desired_is_open
        )

        async def _worker():
            angle = start_angle
            try:
                if target_angle > angle:
                    while angle < target_angle:
                        angle += step
                        if angle > target_angle:
                            angle = target_angle
                        await self._set_angle(angle)
                        if angle == target_angle:
                            break
                        await asyncio.sleep(sleep)
                elif target_angle < angle:
                    while angle > target_angle:
                        angle -= step
                        if angle < target_angle:
                            angle = target_angle
                        await self._set_angle(angle)
                        if angle == target_angle:
                            break
                        await asyncio.sleep(sleep)
                # Final ensure
                await self._set_angle(target_angle)
                logger.info(
                    "Servo soft move complete name=%s final_angle=%s desired_is_open=%s physical_is_open=%s",
                    self.servo.name, angle, self.desired_is_open, self.physical_is_open
                )
            except asyncio.CancelledError:
                logger.debug("Servo soft move worker canceled name=%s", self.servo.name)
                raise
            except Exception as e:
                logger.error("Servo soft move worker error name=%s target=%s error=%s", self.servo.name, target_angle, e)

        self._soft_task = asyncio.create_task(_worker())
        def _done(task: asyncio.Task):
            if task.cancelled():
                return
            exc = task.exception()
            if exc:
                logger.error("Servo soft move task exception name=%s error=%s", self.servo.name, exc)
        self._soft_task.add_done_callback(_done)

class Led_pixel_control(object):
    """Represents a single addressable LED pixel with convenience color setter."""

    def __init__(self, led_name: str, index: int, red: int, green: int, blue: int, moonraker_api: Moonraker_api):
        self.index = index + 1
        self.red = red
        self.green = green
        self.blue = blue
        self.led_name = " ".join(led_name.split(" ")[1:])
        self.moonraker_api = moonraker_api

    async def set_color(self, red: int, green: int, blue: int):
        self.red = red
        self.green = green
        self.blue = blue
        await self.moonraker_api.send_gcode(f"SET_LED LED={self.led_name} INDEX={self.index} RED={red} GREEN={green} BLUE={blue}")

class Led_control(object):
    """Manages an LED strip (group of pixels) including brightness scaling."""

    def __init__(self, moonraker_api: Moonraker_api, led: dryer_schema.LedConfig):
        self.led = led
        self.moonraker_api = moonraker_api
        self.pixels: list[Led_pixel_control] = []
        self.default_color: list = [0.01, 0.01, 0.01]
        self.off_color: list = [0, 0, 0]
        self.external_data: dict = None
        logger.debug("Led_control created name=%s", self.led.name)

    async def _ensure_initialized(self):
        if len(self.pixels) == 0:
            self.pixels = await self._get_pixels()
            for pixel in self.pixels:
                await pixel.set_color(*self.default_color)

    async def update_status(self, external_data: dict = None):
        await self._ensure_initialized()
        self.external_data = external_data
        self.pixels = await self._get_pixels()

    async def set_pixel_color(self, index: int, red: int, green: int, blue: int):
        await self._ensure_initialized()
        index = index + 1
        brightness = self.led.brightness / 100
        red = red * brightness
        green = green * brightness
        blue = blue * brightness
        for pixel in self.pixels:
            if pixel.index == index:
                if pixel.red != red or pixel.green != green or pixel.blue != blue:
                    await pixel.set_color(red=red, green=green, blue=blue)
                break

    async def _get_pixels(self):
        result = await self._get_status()
        pixels = []
        for i in range(len(result)):
            red = result[i][0]
            green = result[i][1]
            blue = result[i][2]
            pixels.append(Led_pixel_control(self.led.name, i, red, green, blue, self.moonraker_api))
        return pixels

    async def _get_status(self):
        if self.external_data == None:
            url = f"{self.moonraker_api.url}/printer/objects/query?{self.led.name}"
            result = await self.moonraker_api.call_api(url)
        else:
            result = self.external_data
            self.external_data = None
        return result['data']['result']['status'][self.led.name]['color_data']

class Heater_fan_control(object):
    """Encapsulates heater fan speed / run-state queries."""

    def __init__(self, moonraker_api: Moonraker_api, fan: dryer_schema.HeaterConfig):
        self.fan = fan
        self.moonraker_api = moonraker_api
        self.speed: float = None
        self.is_run: bool = None
        self.external_data: dict = None
        logger.debug("Heater_fan_control created name=%s", self.fan.fan_name)

    async def update_status(self, external_data: dict = None):
        await self._ensure_initialized()
        self.external_data = external_data
        self.speed = await self._get_status()
        self.is_run = await self._is_run_status(self.speed)

    async def _ensure_initialized(self):
        if self.speed == None:
            self.speed = await self._get_status()
            self.is_run = await self._is_run_status(self.speed)

    async def _is_run_status(self, speed: float):
        if speed > 0:
            return True
        else:
            return False
        
    async def _get_status(self):
        if self.external_data == None:
            url = f"{self.moonraker_api.url}/printer/objects/query?{self.fan.fan_name}"
            result = await self.moonraker_api.call_api(url)
        else:
            result = self.external_data
            self.external_data = None
        return result['data']['result']['status'][self.fan.fan_name]['speed']     

class Heater_control(object):
    """Provides heater temperature / power state management and setpoint changes."""

    def __init__(self, moonraker_api: Moonraker_api, heater: dryer_schema.HeaterConfig):
        self.heater = heater
        self.moonraker_api = moonraker_api
        self.max_temperature: float = None
        self.temperature: float = 0
        self.power: float = 0
        self.target: float = 0
        self.is_on: bool = None
        self.fan = Heater_fan_control(self.moonraker_api, self.heater)
        self.external_data: dict = None
        logger.debug("Heater_control created name=%s", self.heater.name)


    async def update_status(self, external_data: dict = None):
        await self._ensure_initialized()
        self.external_data = external_data
        await self.fan.update_status(self.external_data)
        result = await self._get_status()
        self.temperature = round(result['temperature'], 2)
        self.target = result['target']
        self.power = round(result['power'], 2)
        self.is_on = await self._is_on_status(result['power'])

    async def set(self, target: float):
        heater_name = " ".join(self.heater.name.split(" ")[1:])
        # FIX: выключение должно проходить всегда, минуя _ensure_initialized().
        # Раньше аварийное отключение вызывало _get_max_temp() -> запрос к
        # Moonraker; если связи нет (а это и есть причина аварии), запрос падал,
        # исключение глоталось в _on_data_error, и нагрев НЕ выключался.
        if target == 0:
            if self.target != 0:
                await self.moonraker_api.send_gcode(
                    gcode=f"SET_HEATER_TEMPERATURE HEATER={heater_name} TARGET=0"
                )
                logger.info("Heater OFF name=%s", self.heater.name)
            return
        await self._ensure_initialized()
        # FIX: без известного предела не греем вообще (раньше сравнение
        # `target <= None` бросало TypeError и роняло весь цикл обновления).
        if self.max_temperature is None:
            logger.error(
                "Heater max_temp unknown name=%s -> heating refused (target=%s)",
                self.heater.name, target
            )
            return
        if target <= self.max_temperature and self.target != target:
            gcode = f"SET_HEATER_TEMPERATURE HEATER={heater_name} TARGET={target}"
            await self.moonraker_api.send_gcode(gcode=gcode)

    async def _ensure_initialized(self):
        if self.temperature == None or self.target == None:
            result = await self._get_status()
            self.temperature = result['temperature']
            self.target = result['target']
            self.is_on = await self._is_on_status(result['power'])
        if self.max_temperature == None:
            self.max_temperature = await self._get_max_temp()

    async def _is_on_status(self, power: float):
        if power > 0:
            return True
        else:
            return False

    async def _get_status(self):
        if self.external_data == None:
            url = f"{self.moonraker_api.url}/printer/objects/query?{self.heater.name}"
            result = await self.moonraker_api.call_api(url)
        else:
            result = self.external_data
            self.external_data = None
        return result['data']['result']['status'][self.heater.name]

    async def _get_max_temp(self):
        url = f"{self.moonraker_api.url}/printer/objects/query?configfile"
        result = await self.moonraker_api.call_api(url)
        return result['data']['result']['status']['configfile']['settings'][self.heater.name]['max_temp']

class Temperature_and_humidity_control(object):
    """Tracks temperature & humidity metrics, computing derived statistics.

    Maintains rolling deques plus real-time median filters used for plateau
    detection and control decisions.
    """

    def __init__(self, moonraker_api: Moonraker_api, sensor: dryer_schema.TemperatureConfig, plateau_duration: int):
        self.sensor = sensor
        self.moonraker_api = moonraker_api
        self.temperature = None
        self.relative_humidity = None
        self.absolute_humidity = None
        self.median_relative_humidity_filter = self.RealTimeMedianFilter(5)
        self.median_relative_humidity = None
        self.median_absolute_humidity_filter = self.RealTimeMedianFilter(5)
        self.median_absolute_humidity = None
        self.relative_humidity_values: deque = deque(
            maxlen=plateau_duration)
        self.absolute_humidity_values: deque = deque(
            maxlen=plateau_duration)
        self.external_data: dict = None
        logger.debug("TempHum_control created sensor=%s", self.sensor.sensor_name)

    async def _ensure_initialized(self):
        if self.temperature == None or self.relative_humidity == None or self.absolute_humidity == None or self.median_relative_humidity == None or self.median_absolute_humidity == None:
            result = await self._get_status()
            self.temperature = result['temperature']
            self.relative_humidity = result['humidity']
            self.absolute_humidity = await self._get_absolute_humidity()

    async def update_status(self, external_data: dict = None):
        await self._ensure_initialized()
        self.external_data = external_data
        result = await self._get_status()
        self.temperature = round(result['temperature'], 1)
        self.relative_humidity = round(result['humidity'], 1)
        self.absolute_humidity = await self._get_absolute_humidity()
        self.median_relative_humidity = float(
            self.median_relative_humidity_filter.update(self.relative_humidity))
        self.median_absolute_humidity = float(
            self.median_absolute_humidity_filter.update(self.absolute_humidity))

        self.relative_humidity_values.append(self.median_relative_humidity)
        self.absolute_humidity_values.append(self.median_absolute_humidity)

    async def _get_status(self):
        if self.external_data == None:
            url = f"{self.moonraker_api.url}/printer/objects/query?{self.sensor.sensor_name}"
            result = await self.moonraker_api.call_api(url)
        else:
            result = self.external_data
            self.external_data = None
        return result['data']['result']['status'][self.sensor.sensor_name]  

    async def _get_absolute_humidity(self):
        saturation_vapor_pressure = 6.112 * \
            (2.71828 ** ((17.67 * self.temperature) / (self.temperature + 243.5)))
        absolute_humidity = (saturation_vapor_pressure *
                             self.relative_humidity * 2.1674) / (273.15 + self.temperature)
        absolute_humidity_rounded = round(absolute_humidity, 1)
        return absolute_humidity_rounded

    class RealTimeMedianFilter:
        """Simple fixed-size median filter for streaming values."""

        def __init__(self, window_size):
            self.window_size = window_size
            self.window = deque(maxlen=window_size)

        def update(self, new_value):
            self.window.append(new_value)
            median_value = statistics.median(self.window)
            return median_value

class Heater_PID(object):
    """Wrapper around PID for heater temperature control with dynamic limits."""

    def __init__(self, max_temperature: float):
        self.pid = PID(1, 0.1, 0.05, setpoint=0)
        self.pid.output_limits = (0, 1)
        self.pid.set_auto_mode(True)
        self.min_temperature = 1
        self.max_temperature = max_temperature

    async def update(self, target_temperature: float, min_temperature: float = None, max_temperature: float = None):
        if min_temperature == None:
            min_temperature = self.min_temperature
        if max_temperature == None or max_temperature > self.max_temperature:
            max_temperature = self.max_temperature

        if target_temperature != self.pid.setpoint:
            self.pid.setpoint = target_temperature
        if self.pid.output_limits[0] != min_temperature or self.pid.output_limits[1] != max_temperature:
            self.pid.output_limits = (min_temperature, max_temperature)

    async def get(self, current_temperature: float):
        output = round(self.pid(current_temperature), 2)
        return output

class humidity_PID(object):
    """Computes target heater power indirectly from humidity readings.

    Translates humidity PID output into an equivalent temperature target to
    leverage existing temperature control pathways.
    """

    def __init__(self, target_humidity, min_temperature: float, max_temperature: float):
        self.pid = PID(1, 0.1, 0.05, setpoint=target_humidity)
        self.pid.output_limits = (min_temperature, max_temperature)
        self.pid.set_auto_mode(True)

    async def get(self, curent_humidity: float):
        pid_output = self.pid(curent_humidity)
        min_temperature = self.pid.output_limits[0]
        max_temperature = self.pid.output_limits[1]
        target_temperature = (max_temperature - min_temperature) * (
            (100 - (((pid_output - min_temperature) / (max_temperature - min_temperature)) * 100)) / 100) + min_temperature
        target_temperature_rounded = round(target_temperature, 2)
        return target_temperature_rounded

class Dryer_control(object):
    """Central orchestrator for a single dryer instance.

    Responsibilities:
    * Initialize subsystem controllers
    * Periodically gather batched Moonraker state
    * Apply control algorithms (servo hysteresis, PID loops, storage modes)
    * Persist telemetry logs
    * Manage preset-driven state machine transitions
    """

    def __init__(self, id: int):
        self.id = id
        self.status: dryer_schema.DryerLogStatus = dryer_schema.DryerLogStatus('pending')
        self.current_preset: Optional[preset_schema.Preset] = None
        self.db: Optional[AsyncSession] = None
        self.moonraker_api: Optional[Moonraker_api] = None
        # Fully typed dryer_config; populated in initialize()
        self.dryer_config: Optional[dryer_schema.Dryer] = None
        self.servo: Optional[Servo_control] = None
        self.led: Optional[Led_control] = None
        self.heater: Optional[Heater_control] = None
        self.heater_pid: Optional[Heater_PID] = None
        self.humidity_pid: Optional[humidity_PID] = None
        self.time_drying_start: Optional[datetime] = None
        self.time_left_drying: Optional[int] = None
        self.temperature_and_humidity: Optional[Temperature_and_humidity_control] = None
        # Servo control hysteresis helpers
        self._servo_last_action: Optional[datetime] = None
        self._servo_last_cmd: Optional[float] = None      # monotonic, последняя команда заслонке
        self._ah_hist: deque = deque(maxlen=3600)          # (monotonic, медиана абс. влажности) — для заслонки
        self._vent_ah_min: Optional[float] = None          # минимум влаги с момента закрытия
        self._vent_ah_open: Optional[float] = None         # влага в момент открытия
        self._vent_backoff: float = 0.0
        self._vent_backoff_until: Optional[float] = None
        # Длительности фаз считаем по монотонным часам: у устройства нет RTC,
        # и после синхронизации времени по сети стенные часы прыгают.
        self._timer_mono: Optional[float] = None
        self._drying_mono: Optional[float] = None
        self._at_temp_mono: Optional[float] = None         # прогрев: воздух впервые вышел на температуру
        self._rh_best: Optional[float] = None              # прогрев: лучшая (низшая) влажность
        self._rh_best_mono: Optional[float] = None
        # FIX: сериализует цикл воркера и команды из HTTP-эндпоинтов.
        # Без него Stop/Start посреди _apply_actuator_targets обнулял
        # current_preset / heater_pid, и следующая же строка падала с
        # "'NoneType' object has no attribute 'storage_type'".
        # Лок НЕ реентерабельный: берётся только на внешних входах
        # (update_status и set_status_external), внутренние вызовы
        # set_status() из _apply_actuator_targets его не трогают.
        self._lock = asyncio.Lock()
        logger.info("Dryer instance created id=%s status=%s", self.id, self.status)

    async def initialize(self):
        logger.info("Dryer initialize start id=%s", self.id)
        self.db = get_db()
        async for session in self.db:
            self.moonraker_api = Moonraker_api(session)
            await self.moonraker_api.initialize()
            # Cast to concrete Dryer schema to help static analysis (pylance/pylint)
            self.dryer_config = cast(dryer_schema.Dryer, await dryer_crud.get_dryer_config(session, self.id))
            # Defensive assertion to narrow Optional types for subsequent attribute access
            assert self.dryer_config and self.dryer_config.config, "Dryer config not loaded"
            self.servo = Servo_control(self.moonraker_api, self.dryer_config.config.servo)
            self.led = Led_control(self.moonraker_api, self.dryer_config.config.led)
            self.heater = Heater_control(self.moonraker_api, self.dryer_config.config.heater)
            self.temperature_and_humidity = Temperature_and_humidity_control(
                self.moonraker_api,
                self.dryer_config.config.temperature,
                self.dryer_config.config.humidity.plateau_duration
            )
            logger.info("Dryer initialize done id=%s servo=%s led=%s heater=%s", self.id, self.servo.servo.name, self.led.led.name, self.heater.heater.name)

    async def update_status(self, fast: bool = True) -> json:
        """Внешняя точка входа воркера. Тело вынесено, чтобы взять лок,
        не переписывая отступы всего метода."""
        async with self._lock:
            return await self._update_status_impl(fast)

    async def set_status_external(self, status: dryer_schema.DryerLogStatus, preset: preset_schema.Preset = None):
        """Смена статуса из HTTP-эндпоинта — под тем же локом, что и цикл."""
        async with self._lock:
            await self.set_status(status, preset)

    async def _update_status_impl(self, fast: bool = True) -> json:
        logger.debug("Dryer update_status start id=%s fast=%s status=%s preset=%s", self.id, fast, self.status, getattr(self.current_preset, 'id', None))
        if fast == True:
            url = f"{self.moonraker_api.url}/printer/objects/query?{self.dryer_config.config.servo.name}&{self.dryer_config.config.led.name}&{self.dryer_config.config.heater.name}&{self.dryer_config.config.heater.fan_name}&{self.dryer_config.config.temperature.sensor_name}&"
            result = await self.moonraker_api.call_api(url)
            await self.servo.update_status(result)
            await self.led.update_status(result)
            await self.heater.update_status(result)
            await self.temperature_and_humidity.update_status(result)
        else:
            await self.servo.update_status()
            await self.led.update_status()
            await self.heater.update_status()
            await self.temperature_and_humidity.update_status()
        
        await self._update_led()
        if self.current_preset != None:
            self.db = get_db()
            async for session in self.db:
                preset_link = await preset_crud.get_preset_link(session, self.current_preset.id, self.id)
                current_preset = await preset_crud.get_preset(session, self.current_preset.id)
                if current_preset == None or preset_link == None:
                    logger.warning("Dryer preset missing id=%s preset=%s -> status pending", self.id, getattr(self.current_preset, 'id', None))
                    await self.set_status(dryer_schema.DryerLogStatus.PENDING)
                else:
                    if self.current_preset != current_preset:
                        if (self.current_preset.storage_type == preset_schema.PresetStorageType.HUMIDITY or self.current_preset.storage_type == preset_schema.PresetStorageType.TEMPERATURE) and current_preset.storage_type == preset_schema.PresetStorageType.NONE:
                            if self.status == dryer_schema.DryerLogStatus.HUMIDITY_STORAGE or self.status == dryer_schema.DryerLogStatus.TEMPERATURE_STORAGE:
                                await self.set_status(dryer_schema.DryerLogStatus.PENDING)
                        if (self.status == dryer_schema.DryerLogStatus.TIMER_DRYING and current_preset.humidity < self.temperature_and_humidity.relative_humidity):
                            await self.set_status(dryer_schema.DryerLogStatus.DRYING, current_preset)
                        # FIX v3: потолок блока PID запоминает при создании —
                        # после правки температуры пресета посреди сушки он
                        # оставался старым. Пересоздаём.
                        if (current_preset.temperature != self.current_preset.temperature or
                                current_preset.max_temperature_delta != self.current_preset.max_temperature_delta):
                            self.heater_pid = None
                        self.current_preset = current_preset
                        logger.info("Dryer preset reloaded id=%s preset=%s", self.id, self.current_preset.id)

        await self._apply_actuator_targets()
        self.db = get_db()
        log_data = dryer_schema.DryerLogBase(
            dryer_id = self.id,
            current_preset_id=self.current_preset.id if self.current_preset else None,
            status = self.status,
            timestamp = datetime.utcnow(),
            heater_temperature = self.heater.temperature,
            heater_is_on = self.heater.is_on,
            heater_fan_is_run = self.heater.fan.is_run,
            temperature = self.temperature_and_humidity.temperature,
            servo_is_open = self.servo.desired_is_open,
            absolute_humidity = self.temperature_and_humidity.absolute_humidity,
            relative_humidity = self.temperature_and_humidity.relative_humidity,
            time_left_drying = int(self.time_left_drying) if self.time_left_drying is not None else None
        )
        async for session in self.db:
            result = await dryer_crud.add_log(session, log_data)
            logger.debug(
                "Dryer update_status done id=%s temp=%.2f target=%.2f power=%.2f hum=%.2f relHum=%.2f servoOpen=%s heaterOn=%s fanRun=%s",
                self.id,
                self.heater.temperature,
                self.heater.target,
                self.heater.power,
                self.temperature_and_humidity.absolute_humidity,
                self.temperature_and_humidity.relative_humidity,
                self.servo.desired_is_open,
                self.heater.is_on,
                self.heater.fan.is_run
            )
            return result.json()

    async def set_status(self, status: dryer_schema.DryerLogStatus, preset: preset_schema.Preset = None):
        previous = self.status
        self.status = status
        logger.info("Status change id=%s %s -> %s preset=%s", self.id, previous, self.status, getattr(preset, 'id', None))
        if self.status == dryer_schema.DryerLogStatus.PENDING:
            self.current_preset = None
            self.heater_pid = None
            self.humidity_pid = None
            self.time_left_drying = None
            self.time_drying_start = None
            await self.heater.set(0)
            await self.servo.close()
        elif self.status == dryer_schema.DryerLogStatus.DRYING:
            self.heater_pid = None
            self.humidity_pid = None
            self.time_left_drying = None
            self.time_drying_start = None
            self._timer_mono = None
            self._drying_mono = time.monotonic()
            self._ah_hist.clear()
            self._vent_ah_min = None
            self._vent_ah_open = None
            self._vent_backoff = 0.0
            self._vent_backoff_until = None
            self._at_temp_mono = None
            self._rh_best = None
            self._rh_best_mono = None
            await self.servo.close()
            self.current_preset = preset
        elif self.status == dryer_schema.DryerLogStatus.TIMER_DRYING:
            await self._close_on_phase_change()
            self.time_drying_start = datetime.utcnow()
            self._timer_mono = time.monotonic()
            self.current_preset = preset
        elif self.status == dryer_schema.DryerLogStatus.HUMIDITY_STORAGE:
            await self._close_on_phase_change()
            # FIX: хранение начинается без досушки; нагрев выключается здесь же,
            # а не остаётся на уровне, выставленном в выдержке
            self.time_drying_start = None
            self._timer_mono = None
            self.time_left_drying = None
            await self.heater.set(0)
            self.current_preset = preset
        elif self.status == dryer_schema.DryerLogStatus.TEMPERATURE_STORAGE:
            await self._close_on_phase_change()
            self.current_preset = preset
        logger.debug("Status handlers applied id=%s status=%s current_preset=%s", self.id, self.status, getattr(self.current_preset, 'id', None))

    async def _update_led(self):
        # LED logic is frequent; keep as debug-level summary only.
        current_second = datetime.now().second
        if current_second % 2 == 0:
            leds_is_on = False
        else:
            leds_is_on = True

        #Heater
        if self.heater.is_on == True:
            await self.led.set_pixel_color(0, self.heater.power, 0, 0)
        if self.heater.is_on == False:
            await self.led.set_pixel_color(0, *self.led.default_color)
        #Heater fan
        if self.heater.fan.is_run == True:
            if self.status == dryer_schema.DryerLogStatus.HUMIDITY_STORAGE and leds_is_on == False:
                await self.led.set_pixel_color(1, *self.led.off_color)
            else:
                await self.led.set_pixel_color(1, 0, self.heater.fan.speed, 0)
        if self.heater.fan.is_run == False:
            await self.led.set_pixel_color(1, *self.led.default_color)
        #Humidity
        if self.current_preset != None:
            if (self.status == dryer_schema.DryerLogStatus.TIMER_DRYING or self.status == dryer_schema.DryerLogStatus.HUMIDITY_STORAGE or self.status == dryer_schema.DryerLogStatus.TEMPERATURE_STORAGE) and leds_is_on == False:
                await self.led.set_pixel_color(2, *self.led.off_color)
            else:
                if self.temperature_and_humidity.median_relative_humidity < self.current_preset.humidity:
                    await self.led.set_pixel_color(2, *self.led.default_color)
                else:
                    await self.led.set_pixel_color(2, 0, 0, self.temperature_and_humidity.median_relative_humidity / 100)
        else:
            await self.led.set_pixel_color(2, *self.led.default_color)
        #Temperature
        if self.status == dryer_schema.DryerLogStatus.TEMPERATURE_STORAGE and leds_is_on == False:
            await self.led.set_pixel_color(3, *self.led.off_color)
        else:
            if self.temperature_and_humidity.temperature <= 50:
                await self.led.set_pixel_color(3, *self.led.default_color)
            else:
                # FIX: max_temperature может быть None (Moonraker недоступен на
                # старте) или 0 -> ZeroDivisionError/TypeError валил весь цикл
                # обновления из-за подсветки.
                if self.heater.max_temperature and self.heater.temperature is not None:
                    level = max(0.0, min(1.0, self.heater.temperature / self.heater.max_temperature))
                    await self.led.set_pixel_color(3, level, 0, 0)
                else:
                    await self.led.set_pixel_color(3, *self.led.default_color)
        logger.debug("LED update applied id=%s leds_on=%s heater_on=%s fan=%s servo_open=%s", self.id, leds_is_on, self.heater.is_on, self.heater.fan.is_run, self.servo.desired_is_open)

    async def _apply_actuator_targets(self):
        # Decide and apply control outputs based on status
        logger.debug("Apply actuators start id=%s status=%s", self.id, self.status)
        if self.status == dryer_schema.DryerLogStatus.PENDING:
            # FIX v3: проверяем уставку, а не «греет ли сейчас»: после
            # перезапуска PyUnit посреди сушки уставка в Klipper остаётся, и
            # пока блок горячее её, is_on == False — нагрев не снимался.
            # heater.set(0) сам ничего не шлёт, если уставка уже 0.
            if self.heater.target:
                logger.info("Heater off due to PENDING id=%s", self.id)
            await self.heater.set(0)
            return
        # FIX: страховка от рассинхрона состояния — любой не-PENDING режим
        # обязан иметь пресет. Раньше отсутствие пресета роняло цикл.
        if self.current_preset is None:
            logger.warning("Actuators skipped: no preset id=%s status=%s -> PENDING", self.id, self.status)
            await self.set_status(dryer_schema.DryerLogStatus.PENDING)
            return
        # FIX: единая точка расчёта потолка + гарантия, что PID существует
        # в любом рабочем режиме (раньше HUMIDITY_STORAGE/TIMER_DRYING могли
        # обратиться к self.heater_pid == None после Start/Stop не вовремя).
        max_temperature = self.current_preset.temperature + self.current_preset.max_temperature_delta
        if self.heater.max_temperature is None:
            logger.error("Heater max_temp unknown id=%s -> actuators skipped", self.id)
            return
        if max_temperature > self.heater.max_temperature:
            max_temperature = self.heater.max_temperature
        if self.heater_pid is None:
            self.heater_pid = Heater_PID(max_temperature)

        rh = self.temperature_and_humidity.median_relative_humidity
        if rh is None or self.temperature_and_humidity.temperature is None:
            # FIX: без показаний датчика камеры регулировать нечем — раньше
            # цикл падал на сравнении с None, оставляя нагреватель как был.
            logger.warning("Chamber sensor has no data id=%s -> heater off", self.id)
            await self.heater.set(0)
            return
        ah = self.temperature_and_humidity.median_absolute_humidity
        if ah is not None:
            self._ah_hist.append((time.monotonic(), ah))
        # FIX v3: коридор хранения — из пресета («допустимый уход влажности
        # при хранении»); оригинал это поле не читал. 0 — общий из настроек.
        storage_range = self.current_preset.humidity_storage_range or \
            self.dryer_config.config.humidity.timer_drying_range
        band_hi = self.current_preset.humidity + storage_range

        # FIX v2: на этапах сушки нагрев всегда держит ВОЗДУХ на температуре
        # пресета. Раньше на выдержке нагрев шёл «по влажности»: как только
        # относительная влажность опускалась до цели (на жаре это минуты),
        # нагреватель выключался, хотя катушка сохнет только в тепле.
        if self.status == dryer_schema.DryerLogStatus.DRYING:
            await self._heat_to(self.current_preset.temperature, max_temperature)
            await self._vent_control()
            now = time.monotonic()
            if abs(self.temperature_and_humidity.temperature - self.current_preset.temperature) <= DRY_AT_TEMP_C \
                    and self._at_temp_mono is None:
                self._at_temp_mono = now
            if self._rh_best is None or rh < self._rh_best - DRY_PLATEAU_RH:
                self._rh_best, self._rh_best_mono = rh, now
            if self.current_preset.humidity >= rh:
                await self.set_status(dryer_schema.DryerLogStatus.TIMER_DRYING, self.current_preset)
            elif self._at_temp_mono is not None and now - self._at_temp_mono >= DRY_PLATEAU_S \
                    and now - self._rh_best_mono >= DRY_PLATEAU_S:
                # FIX v3: воздух на температуре, а влажность давно не падает —
                # ниже она при этом нагреве уже не станет
                logger.info("Humidity plateau %.1f%% (target %s%%) at temperature -> timer phase id=%s",
                            rh, self.current_preset.humidity, self.id)
                await self.set_status(dryer_schema.DryerLogStatus.TIMER_DRYING, self.current_preset)
            elif self._drying_mono is not None and \
                    time.monotonic() - self._drying_mono >= self.current_preset.dry_time * 60:
                # если цель по влажности недостижима (сырая комната, мокрая
                # катушка), прогрев не должен длиться бесконечно
                logger.warning("Humidity target %s%% not reached in %s min (now %.1f%%) -> timer phase id=%s",
                               self.current_preset.humidity, self.current_preset.dry_time, rh, self.id)
                await self.set_status(dryer_schema.DryerLogStatus.TIMER_DRYING, self.current_preset)

        elif self.status == dryer_schema.DryerLogStatus.TIMER_DRYING:
            await self._heat_to(self.current_preset.temperature, max_temperature)
            await self._vent_control()
            if self._timer_mono is None:
                self._timer_mono = time.monotonic()
                self.time_drying_start = datetime.utcnow()
            elapsed = time.monotonic() - self._timer_mono
            self.time_left_drying = self.current_preset.dry_time * 60 - elapsed
            if elapsed >= self.current_preset.dry_time * 60:
                self.time_left_drying = None
                if self.current_preset.storage_type == preset_schema.PresetStorageType.HUMIDITY:
                    await self.set_status(dryer_schema.DryerLogStatus.HUMIDITY_STORAGE, self.current_preset)
                elif self.current_preset.storage_type == preset_schema.PresetStorageType.TEMPERATURE:
                    await self.set_status(dryer_schema.DryerLogStatus.TEMPERATURE_STORAGE, self.current_preset)
                else:
                    await self.set_status(dryer_schema.DryerLogStatus.PENDING)

        elif self.status == dryer_schema.DryerLogStatus.HUMIDITY_STORAGE:
            # Хранение: в простое нагрев выключен и заслонка закрыта. Если
            # влажность вышла за коридор — досушка при температуре пресета
            # на humidity_storage_dry_time (не меньше минуты).
            cycle_s = max(self.current_preset.humidity_storage_dry_time, 1) * 60
            if self._timer_mono is None:
                if rh > band_hi:
                    self._timer_mono = time.monotonic()
                    self.time_drying_start = datetime.utcnow()
                    logger.info("Storage re-dry start id=%s rh=%.1f > %.1f", self.id, rh, band_hi)
                else:
                    await self.heater.set(0)
                    if self.servo.desired_is_open and not self._servo_moving():
                        await self._servo_cmd(False, 'storage idle')
                    self.time_left_drying = None
                    return
            await self._heat_to(self.current_preset.temperature, max_temperature)
            await self._vent_control()
            elapsed = time.monotonic() - self._timer_mono
            self.time_left_drying = cycle_s - elapsed
            if elapsed >= cycle_s:
                await self.heater.set(0)
                if self.servo.desired_is_open:
                    await self._servo_cmd(False, 'storage cycle end')
                self._timer_mono = None
                self.time_drying_start = None
                self.time_left_drying = None

        elif self.status == dryer_schema.DryerLogStatus.TEMPERATURE_STORAGE:
            storage_target = self.current_preset.storage_temperature
            if not (storage_target and storage_target > 0):
                storage_target = self.current_preset.temperature
            await self._heat_to(storage_target, max_temperature)
            await self._vent_control()

    # ------------------------------------------------------------------ нагрев
    async def _heat_to(self, air_target: float, max_temperature: float):
        """Держать ВОЗДУХ на air_target: PID по датчику камеры задаёт уставку
        блока нагревателя (не выше max_temperature)."""
        air = self.temperature_and_humidity.temperature
        if air >= max_temperature + AIR_OVERHEAT_C:
            # Воздух не может быть горячее блока, а блок не выше потолка.
            # Раз так — датчик или нагреватель ведут себя не так, как думаем.
            logger.error("Air %.1f C above heater ceiling %.1f C id=%s -> heater off", air, max_temperature, self.id)
            await self.heater.set(0)
            return
        await self.heater_pid.update(air_target, 1, max_temperature)
        await self.heater.set(await self.heater_pid.get(self.temperature_and_humidity.temperature))

    # ------------------------------------------------------------------ заслонка
    def _servo_moving(self) -> bool:
        task = self.servo._soft_task
        return task is not None and not task.done()

    async def _servo_cmd(self, open_: bool, reason: str):
        if open_:
            await self.servo.open()
        else:
            await self.servo.close()
            self._vent_ah_min = None
        self._servo_last_cmd = time.monotonic()
        self._servo_last_action = datetime.utcnow()
        logger.info("Servo %s id=%s (%s)", 'OPEN' if open_ else 'CLOSE', self.id, reason)

    async def _close_on_phase_change(self):
        """Смена этапа закрывает заслонку; если она была открыта
        проветриванием — через _servo_cmd, чтобы учёт проветривания не
        считал её открытой и начал копить влагу заново."""
        if self.servo.desired_is_open:
            await self._servo_cmd(False, 'phase change')
        else:
            await self.servo.close()

    def _vent_rise(self) -> float:
        """Порог «накопилось влаги»: не меньше двух шагов округления датчика."""
        t = self.temperature_and_humidity.temperature or 0.0
        return max(SERVO_AH_RISE, 2 * _ah_per_rh_step(t))

    def _ah_slope(self, since: float):
        """Наклон абсолютной влажности, г/м³ в минуту, по точкам не раньше since."""
        pts = [(t, v) for t, v in self._ah_hist if t >= since]
        if len(pts) < 10:
            return None
        n = float(len(pts))
        mt = sum(t for t, _ in pts) / n
        mv = sum(v for _, v in pts) / n
        sxx = sum((t - mt) ** 2 for t, _ in pts)
        if sxx <= 0:
            return None
        sxy = sum((t - mt) * (v - mv) for t, v in pts)
        return sxy / sxx * 60.0

    async def _vent_control(self):
        """Проветривание камеры.

        Закрыта: катушка отдаёт воду, и её количество в воздухе (абсолютная
        влажность) растёт. Накопилось SERVO_AH_RISE с момента закрытия ->
        открыть и выпустить влажный воздух.
        Открыта: влага уходит; когда падение остановилось (или открыта уже
        SERVO_MAX_OPEN_S) -> закрыть. Если проветривание почти ничего не
        дало (в комнате воздух не суше, чем в камере), следующее откладываем,
        с каждым разом дольше — чтобы не гонять заслонку и не терять тепло.

        FIX v2: раньше решение принималось по относительной влажности. Она
        падает просто от нагрева, поэтому на прогреве и выдержке заслонка
        почти никогда не открывалась.
        """
        if self._servo_moving():
            return
        ah = self.temperature_and_humidity.median_absolute_humidity
        if ah is None:
            return
        now = time.monotonic()
        last = self._servo_last_cmd
        if last is not None and now - last < SERVO_SETTLE_S:
            return
        if not self.servo.desired_is_open:
            if self._vent_ah_min is None or ah < self._vent_ah_min:
                self._vent_ah_min = ah
            if last is not None and now - last < SERVO_MIN_DWELL_S:
                return
            if self._vent_backoff_until is not None and now < self._vent_backoff_until:
                return
            if ah - self._vent_ah_min >= self._vent_rise():
                self._vent_ah_open = ah
                await self._servo_cmd(True, 'vent: water %.1f g/m3, +%.1f since close' % (ah, ah - self._vent_ah_min))
            return
        open_for = now - last if last is not None else SERVO_MAX_OPEN_S
        if open_for < SERVO_MIN_DWELL_S:
            return
        since = (last if last is not None else now) + SERVO_SETTLE_S
        slope = None
        if now - since >= SERVO_TREND_S:
            slope = self._ah_slope(now - SERVO_TREND_S)
        stalled = slope is not None and slope > -SERVO_AH_FALL
        if not (stalled or open_for >= SERVO_MAX_OPEN_S):
            return
        gain = (self._vent_ah_open - ah) if self._vent_ah_open is not None else 0.0
        if gain < self._vent_rise() / 2:
            self._vent_backoff = min(max(self._vent_backoff * 2, SERVO_BACKOFF_S[0]), SERVO_BACKOFF_S[1])
        else:
            self._vent_backoff = 0.0
        self._vent_backoff_until = now + self._vent_backoff if self._vent_backoff else None
        await self._servo_cmd(False, 'vent done: water %.1f g/m3 (-%.1f), next pause %ds'
                              % (ah, gain, int(self._vent_backoff)))
