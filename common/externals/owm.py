from msu_hub_bot.settings import settings

import datetime
import io
import math
from typing import List, Tuple, Optional

import aiohttp
from PIL import Image, ImageOps
from aiocache import cached
from pydantic import BaseModel

from common import json
from common.utils import bytes_io, image_bytes_io, is_en


class WeatherType(BaseModel):
    id: int
    main: str
    description: str
    icon: str


class CurrentWeather(BaseModel):
    dt: datetime.datetime
    sunrise: Optional[int]
    sunset: Optional[int]
    temp: float
    feels_like: float
    pressure: int
    humidity: int
    dew_point: float
    uvi: Optional[float]
    clouds: int
    visibility: int
    wind_speed: int
    wind_deg: int
    weather: List[WeatherType]


class MinutelyWeather(BaseModel):
    dt: datetime.datetime
    precipitation: int


class HourlyWeather(BaseModel):
    dt: datetime.datetime
    temp: float
    feels_like: float
    pressure: int
    humidity: int
    dew_point: float
    clouds: int
    visibility: int
    wind_speed: float
    wind_deg: int
    weather: List[WeatherType]
    pop: float


class Temp(BaseModel):
    day: float
    min: float
    max: float
    night: float
    eve: float
    morn: float


class FeelsLike(BaseModel):
    day: float
    night: float
    eve: float
    morn: float


class DailyWeather(BaseModel):
    dt: datetime.datetime
    sunrise: int
    sunset: int
    temp: Temp
    feels_like: FeelsLike
    pressure: int
    humidity: int
    dew_point: float
    wind_speed: float
    wind_deg: int
    weather: List[WeatherType]
    clouds: int
    pop: float
    uvi: Optional[float]


class Alert(BaseModel):
    sender_name: str
    event: str
    start: int
    end: int
    description: str


class ResponseOneCall(BaseModel):
    lat: float
    lon: float
    timezone: str
    timezone_offset: int
    current: CurrentWeather
    minutely: Optional[List[MinutelyWeather]]
    hourly: List[HourlyWeather]
    daily: List[DailyWeather]
    alerts: Optional[List[Alert]]


def id_to_emoji(weather_id: int) -> str:
    # Condition codes: https://openweathermap.org/weather-conditions

    if weather_id in range(200, 203):
        return '⛈'

    if weather_id in range(230, 233):
        return '⛈'

    if weather_id in range(200, 300):
        return '🌩'

    if weather_id in range(300, 400):
        return '🌦'

    if weather_id in range(600, 700) or weather_id == 511:
        return '❄️'

    if weather_id in range(500, 600):
        return '☔️'

    if weather_id == 781:
        return '🌪'

    if weather_id in range(700, 800):
        return '🌫'

    if weather_id == 800:
        return '☀️'

    if weather_id == 801:
        return '🌤'

    if weather_id == 802:
        return '⛅️'

    if weather_id == 803:
        return '🌥'

    if weather_id in range(804, 900):
        return '☁️'

    return '☁️'


def coordinates_to_xy(coordinates: Tuple[float, float], zoom: int):
    # https://wiki.openstreetmap.org/wiki/Slippy_map_tilenames
    lat_rad = math.radians(coordinates[0])
    n = 2.0 ** zoom
    x_tile = int((coordinates[1] + 180.0) / 360.0 * n)
    y_tile = int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n)
    return x_tile, y_tile


@cached(ttl=2 * 60, key_builder=lambda f, coords, __: (f.__name__, round(coords[0], 3), round(coords[1], 3)))
async def weather(coordinates: Tuple[float, float], location_name: str = None) -> Optional[Tuple[ResponseOneCall, str]]:
    # API Docs: https://openweathermap.org/api

    api_base = 'https://api.openweathermap.org/data/2.5/'
    params = dict(lat=str(coordinates[0]), lon=str(coordinates[1]),
                  units='metric', lang='ru', appid=settings.require('owm_key'))

    async with aiohttp.ClientSession() as session:
        async with session.get(api_base + 'onecall', params=params) as response:
            if response.status != 200:
                return None
            one_call = await response.read()

        if not location_name:
            async with session.get(api_base + 'weather', params=params) as response:
                if response.status != 200:
                    return None
                location_name = json.loads(await response.read())['name']

    return ResponseOneCall.parse_raw(one_call), location_name


async def geocoding(name: str) -> Optional[Tuple[Tuple[float, float], str]]:
    # API Docs: https://docs.mapbox.com/api/search/geocoding/#forward-geocoding

    mapbox_key = settings.require('mapbox_key')
    search_text = name[:256]
    language = 'en,ru' if is_en(search_text) else 'ru,en'

    async with aiohttp.ClientSession() as session:
        mapbox_url = f'https://api.mapbox.com/geocoding/v5/mapbox.places/{search_text}.json'
        async with session.get(mapbox_url, params=dict(access_token=mapbox_key, autocomplete='false', types='place', language=language, limit='1')) as response:
            if response.status != 200:
                return None
            result = await response.json()

    features = result.get('features', [])
    if not features:
        return None

    center = features[0]['center']
    return (center[1], center[0]), features[0]['place_name_ru']


@cached(ttl=2 * 60)
async def weather_map(x: int, y: int, zoom: int = 13) -> Optional[io.BytesIO]:
    # OWM Docs: https://openweathermap.org/api/weathermaps
    # Mapbox Docs: https://docs.mapbox.com/api/maps/#static-tiles

    owm_key = settings.require('owm_map_key')
    mapbox_key = settings.require('mapbox_key')

    async with aiohttp.ClientSession() as session:
        mapbox_url = f'https://api.mapbox.com/styles/v1/mapbox/satellite-streets-v11/tiles/512/{zoom}/{x}/{y}?access_token={mapbox_key}'
        async with session.get(mapbox_url) as response:
            if response.status != 200:
                return None
            content = await response.read()
            background = Image.open(bytes_io(content)).convert('RGBA')

        owm_url = f'https://tile.openweathermap.org/map/precipitation_new/{zoom}/{x}/{y}.png?appid={owm_key}'
        async with session.get(owm_url) as response:
            if response.status != 200:
                return None
            content = await response.read()
            layer = Image.open(bytes_io(content)).convert('RGBA')

    layer = ImageOps.fit(layer, (background.width, background.height), Image.LANCZOS)
    background.alpha_composite(layer)
    return image_bytes_io(background, filename='weather_map', ext='png')
