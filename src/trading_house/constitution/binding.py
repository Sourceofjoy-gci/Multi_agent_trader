"""The signed, venue-specific projection of neutral book and instrument ids."""

from itertools import combinations
from pathlib import Path
from typing import Literal, Self

import yaml
from pydantic import Field, PositiveInt, ValidationError, field_validator, model_validator

from trading_house.constitution.loader import load_signed
from trading_house.constitution.models import ConstitutionModel
from trading_house.core.errors import ConfigurationError
from trading_house.core.values import BookId, InstrumentId, NonEmptyStr


class BookBinding(ConstitutionModel):
    magic_range: tuple[PositiveInt, PositiveInt]

    @field_validator("magic_range", mode="before")
    @classmethod
    def convert_magic_range_sequence(cls, value: object) -> object:
        if isinstance(value, list):
            return tuple(value)
        return value

    @model_validator(mode="after")
    def range_is_ordered(self) -> Self:
        if self.magic_range[0] >= self.magic_range[1]:
            raise ValueError("magic_range must be ascending")
        return self


class InstrumentBinding(ConstitutionModel):
    server_symbol: NonEmptyStr


class VenueBinding(ConstitutionModel):
    venue: Literal["mt5"]
    books: dict[BookId, BookBinding] = Field(min_length=1)
    instruments: dict[InstrumentId, InstrumentBinding] = Field(min_length=1)

    @model_validator(mode="after")
    def magic_ranges_do_not_overlap(self) -> Self:
        for (_, left), (_, right) in combinations(self.books.items(), 2):
            if (
                left.magic_range[0] <= right.magic_range[1]
                and right.magic_range[0] <= left.magic_range[1]
            ):
                raise ValueError("magic ranges must not overlap")
        return self


def parse_venue_binding(data: bytes) -> VenueBinding:
    try:
        parsed = yaml.safe_load(data)
        if not isinstance(parsed, dict):
            raise ConfigurationError()
        return VenueBinding.model_validate(parsed)
    except (yaml.YAMLError, UnicodeDecodeError, ValidationError) as error:
        raise ConfigurationError() from error


def load_venue_binding(
    binding_path: Path, signature_path: Path, public_key_path: Path
) -> VenueBinding:
    """Verify the binding signature before parsing it."""

    return parse_venue_binding(load_signed(binding_path, signature_path, public_key_path).content)
