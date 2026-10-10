from dataclasses import dataclass
from decimal import Decimal
from app.search import canon


@dataclass(frozen=True)
class Unit:
    name: str
    dimension: str
    factor: Decimal


_GROUPS = (
    ("length", (("мм", ".001", ()), ("см", ".01", ("cм",)), ("м", "1", ()))),
    ("area", (("м²", "1", ("м2",)), ("см²", ".0001", ("см2",)))),
    ("volume", (("м³", "1", ("м3",)), ("л", ".001", ("литр", "литра", "литров", "дм³")), ("мл", ".000001", ()))),
    ("mass", (("кг", "1", ()), ("г", ".001", ()))),
    ("power", (("Вт", "1", ("вт",)), ("кВт", "1000", ("квт",)))),
    ("voltage", (("В", "1", ("в",)),)),
    ("resistance", (("Ом", "1", ("ом", "ω", "Ω")),)),
    ("percentage", (("%", "1", ("процент",)),)),
    ("rotation_rate", (("об/мин", "1", ("об./мин", "об / мин")),)),
    ("time", (("мин", "60", ()), ("с", "1", ()))),
    ("surface_density", (("г/м²", "1", ("г/м2",)),)),
    ("charge", (("А·ч", "1", ("а*ч", "ач", "а·ч")),)),
    ("temperature", (("°C", "1", ("°с", "°c")),)),
    ("count", (("шт", "1", ("штука", "штук", "штуки")),)),
    ("unit_count", (("ед", "1", ("единица",)),)),
    ("pair_count", (("пар", "1", ("пара",)),)),
)
UNITS = {canon(alias): Unit(name, dimension, Decimal(factor))
         for dimension, rows in _GROUPS for name, factor, aliases in rows for alias in (name, *aliases)}


def get_unit(name: str | None) -> Unit | None:
    return UNITS.get(canon(name)) if name else None
