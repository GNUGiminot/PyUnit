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

# --- Управление заслонкой (см. Dryer_control._servo_control) -----------------
# Окно оценки тренда влажности. Меньше нельзя: за 30 с влажность в камере
# физически почти не меняется, и решение принималось по шуму датчика
# (заслонка открывалась и закрывалась каждые ~30 с, не успевая проветрить).
SERVO_MIN_WINDOW_S = 180
# После доезда заслонка стоит в новом положении хотя бы столько, прежде чем
# снова принимать решение (ход 125->30 занимает ~16 с).
SERVO_SETTLE_S = 20
SERVO_MIN_DWELL_S = 60
# Гистерезис для режимов «держать влажность в коридоре», %RH: без него при
# влажности на пороге заслонка разворачивалась каждую секунду.
SERVO_BAND_HYST = 0.3

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
        self._rh_hist: deque = deque(maxlen=3600)          # (monotonic, медиана RH) — тренд для заслонки
        # Длительности фаз считаем по монотонным часам: у устройства нет RTC,
        # и после синхронизации времени по сети стенные часы прыгают.
        self._timer_mono: Optional[float] = None
        self._drying_mono: Optional[float] = None
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
            self._rh_hist.clear()
            await self.servo.close()
            self.current_preset = preset
        elif self.status == dryer_schema.DryerLogStatus.TIMER_DRYING:
            await self.servo.close()
            self.time_drying_start = datetime.utcnow()
            self._timer_mono = time.monotonic()
            self.current_preset = preset
        elif self.status == dryer_schema.DryerLogStatus.HUMIDITY_STORAGE:
            await self.servo.close()
            # FIX: хранение начинается без досушки; нагрев выключается здесь же,
            # а не остаётся на уровне, выставленном в выдержке
            self.time_drying_start = None
            self._timer_mono = None
            self.time_left_drying = None
            await self.heater.set(0)
            self.current_preset = preset
        elif self.status == dryer_schema.DryerLogStatus.TEMPERATURE_STORAGE:
            await self.servo.close()
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
            if self.heater.is_on == True:
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
        self._rh_hist.append((time.monotonic(), rh))
        band = self.dryer_config.config.humidity.timer_drying_range
        band_hi = self.current_preset.humidity + band
        band_lo = self.current_preset.humidity - band

        if self.status == dryer_schema.DryerLogStatus.DRYING:
            await self._heat_to(self.current_preset.temperature, max_temperature)
            await self._servo_control()
            if self.current_preset.humidity >= rh:
                await self.set_status(dryer_schema.DryerLogStatus.TIMER_DRYING, self.current_preset)
            elif self._drying_mono is not None and \
                    time.monotonic() - self._drying_mono >= self.current_preset.dry_time * 60:
                # FIX: если цель по влажности недостижима (сырая комната, мокрая
                # катушка), прогрев длился бесконечно. Ограничиваем его временем
                # пресета и переходим к отсчёту — сушка заведомо закончится.
                logger.warning("Humidity target %s%% not reached in %s min (now %.1f%%) -> timer phase id=%s",
                               self.current_preset.humidity, self.current_preset.dry_time, rh, self.id)
                await self.set_status(dryer_schema.DryerLogStatus.TIMER_DRYING, self.current_preset)

        elif self.status == dryer_schema.DryerLogStatus.TIMER_DRYING:
            if rh > band_hi:
                await self._heat_to(self.current_preset.temperature, max_temperature)
                await self._servo_control()
            else:
                await self._heat_by_humidity(max_temperature)
                await self._servo_band(rh, band_lo)
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
            # FIX: в простое (влажность в коридоре) нагрев раньше не выключался —
            # оставалась уставка из выдержки, и камера часами стояла горячей.
            # Теперь: досушка запускается, только когда влажность вышла за
            # коридор, длится humidity_storage_dry_time (не меньше минуты),
            # а в простое нагрев выключен и заслонка закрыта.
            cycle_s = max(self.current_preset.humidity_storage_dry_time, 1) * 60
            if self._timer_mono is None:
                if rh > band_hi:
                    self._timer_mono = time.monotonic()
                    self.time_drying_start = datetime.utcnow()
                    logger.info("Storage re-dry start id=%s rh=%.1f > %.1f", self.id, rh, band_hi)
                else:
                    await self.heater.set(0)
                    if self.servo.desired_is_open:
                        await self._servo_cmd(False, 'storage idle')
                    self.time_left_drying = None
                    return
            if rh > band_hi:
                await self._heat_to(self.current_preset.temperature, max_temperature)
                await self._servo_control()
            else:
                await self._heat_by_humidity(max_temperature)
                await self._servo_band(rh, band_lo)
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
            if rh > self.current_preset.humidity:
                await self._servo_control()
            else:
                await self._servo_band(rh, self.current_preset.humidity)

    # ------------------------------------------------------------------ нагрев
    async def _heat_to(self, air_target: float, max_temperature: float):
        """Держать ВОЗДУХ на air_target: PID по датчику камеры задаёт уставку
        блока нагревателя (не выше max_temperature)."""
        await self.heater_pid.update(air_target, 1, max_temperature)
        await self.heater.set(await self.heater_pid.get(self.temperature_and_humidity.temperature))

    async def _heat_by_humidity(self, max_temperature: float):
        """Нагрев по влажности внутри коридора.

        FIX: раньше результат humidity-PID шёл прямо в уставку блока
        нагревателя (до temperature + delta), минуя регулятор температуры
        воздуха, и воздух перегревался выше заданного пресетом (у PETG до
        ~70 °C при заданных 60). Теперь влажность задаёт уставку ВОЗДУХА в
        пределах 1..temperature пресета, а блок по-прежнему ведёт PID воздуха.
        """
        air_max = float(self.current_preset.temperature)
        if self.humidity_pid is None or tuple(self.humidity_pid.pid.output_limits) != (1, air_max):
            self.humidity_pid = humidity_PID(self.current_preset.humidity, 1, air_max)
        air_target = await self.humidity_pid.get(self.temperature_and_humidity.median_relative_humidity)
        if air_target <= 1:
            # влажность ниже цели — греть не нужно, нагреватель выключаем явно
            await self.heater.set(0)
            return
        await self._heat_to(air_target, max_temperature)

    # ------------------------------------------------------------------ заслонка
    def _servo_moving(self) -> bool:
        task = self.servo._soft_task
        return task is not None and not task.done()

    async def _servo_cmd(self, open_: bool, reason: str):
        if open_:
            await self.servo.open()
        else:
            await self.servo.close()
        self._servo_last_cmd = time.monotonic()
        self._servo_last_action = datetime.utcnow()
        logger.info("Servo %s id=%s (%s)", 'OPEN' if open_ else 'CLOSE', self.id, reason)

    def _rh_slope(self, since: float):
        """Наклон медианы влажности, %RH в минуту, по точкам не раньше since."""
        pts = [(t, v) for t, v in self._rh_hist if t >= since and v is not None]
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

    async def _servo_band(self, rh: float, low: float):
        """Режим «держать влажность в коридоре»: открыть ниже нижней границы.

        FIX: раньше команда давалась каждый тик без гистерезиса, и на пороге
        заслонка разворачивалась каждую секунду. Теперь — гистерезис, запрет
        решений во время хода и минимальное время стояния.
        """
        if self._servo_moving():
            return
        if self._servo_last_cmd is not None and time.monotonic() - self._servo_last_cmd < SERVO_MIN_DWELL_S:
            return
        if not self.servo.desired_is_open and rh < low:
            await self._servo_cmd(True, 'band: rh %.1f < %.1f' % (rh, low))
        elif self.servo.desired_is_open and rh >= low + SERVO_BAND_HYST:
            await self._servo_cmd(False, 'band: rh %.1f >= %.1f' % (rh, low + SERVO_BAND_HYST))

    async def _servo_control(self):
        """Проветривание при сушке.

        Замысел PyUnit: закрытая камера набирает влагу -> когда влажность
        перестаёт падать, открыть и выпустить влажный воздух -> когда падение
        от проветривания остановилось, закрыть.

        FIX: прежняя реализация решала по окну 30 с, то есть по шуму датчика
        (на устройстве сотни открытий за сушку, открытой заслонка стояла
        секунды), и НЕ открывала заслонку при росте влажности в закрытой
        камере — именно когда катушка отдаёт влагу и проветривать нужнее всего.
        Теперь тренд считается линейной регрессией за окно не меньше
        SERVO_MIN_WINDOW_S, причём только по данным после того, как заслонка
        встала в текущее положение. «Падение остановилось» = наклон не круче
        -порог/окно; это условие и открывает закрытую, и закрывает открытую.
        """
        cfg = self.dryer_config.config.humidity
        if self._servo_moving():
            return
        now = time.monotonic()
        window = max(float(cfg.plateau_duration or 0), float(SERVO_MIN_WINDOW_S))
        since = (self._servo_last_cmd + SERVO_SETTLE_S) if self._servo_last_cmd is not None else None
        if not self._rh_hist or now - self._rh_hist[0][0] < window:
            return                                   # мало истории после старта
        if since is not None and now - since < max(window, SERVO_MIN_DWELL_S):
            return                                   # после движения ещё не набралось окно
        start = max(now - window, since if since is not None else now - window)
        slope = self._rh_slope(start)
        if slope is None:
            return
        threshold = max(float(cfg.open_threshold or 0), 0.2) / (window / 60.0)   # %RH/мин
        stalled = slope > -threshold
        if not stalled:
            return
        if self.servo.desired_is_open:
            await self._servo_cmd(False, 'vent: fall stalled, slope %.3f %%/min' % slope)
        else:
            await self._servo_cmd(True, 'vent: rh not falling, slope %.3f %%/min' % slope)

