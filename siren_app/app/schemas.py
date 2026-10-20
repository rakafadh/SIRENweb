"""Request bodies. Ranges reject values no living patient or working sensor produces."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

Acvpu = Literal["alert", "confusion", "voice", "pain", "unresponsive"]
Sex = Literal["Laki-laki", "Perempuan"]

HR_RANGE = (20, 250)
SPO2_RANGE = (50, 100)
RR_RANGE = (4, 60)
TEMP_RANGE = (25, 45)
SBP_RANGE = (40, 300)
DBP_RANGE = (20, 200)


def _check_bp(systolic: float | None, diastolic: float | None) -> None:
    if diastolic is not None and systolic is None:
        raise ValueError("Isi tekanan sistolik bersama diastolik.")
    if systolic is not None and diastolic is not None and diastolic >= systolic:
        raise ValueError("Diastolik harus lebih rendah dari sistolik.")


class PatientCreate(BaseModel):
    name: str | None = Field(default=None, max_length=150)
    age: int | None = Field(default=None, ge=0, le=120)
    sex: Sex | None = None
    complaint: str | None = Field(default=None, max_length=500)
    comorbid: bool = False
    destination: str | None = Field(default=None, max_length=160)
    crew: str | None = Field(default=None, max_length=120)

    ambulatory: bool = False
    uncontrolled_bleeding: bool = False
    limb_loss_risk: bool = False
    chest_pain: bool = False

    acvpu: Acvpu = "alert"
    on_oxygen: bool = False
    pain: int | None = Field(default=None, ge=0, le=10)
    systolic: float | None = Field(default=None, ge=SBP_RANGE[0], le=SBP_RANGE[1])
    diastolic: float | None = Field(default=None, ge=DBP_RANGE[0], le=DBP_RANGE[1])

    @model_validator(mode="after")
    def _bp(self):
        _check_bp(self.systolic, self.diastolic)
        return self


class PatientUpdate(BaseModel):
    """Partial update. Only fields present in the request are applied."""

    name: str | None = Field(default=None, max_length=150)
    age: int | None = Field(default=None, ge=0, le=120)
    sex: Sex | None = None
    complaint: str | None = Field(default=None, max_length=500)
    comorbid: bool | None = None
    destination: str | None = Field(default=None, max_length=160)
    crew: str | None = Field(default=None, max_length=120)

    ambulatory: bool | None = None
    uncontrolled_bleeding: bool | None = None
    limb_loss_risk: bool | None = None
    chest_pain: bool | None = None

    acvpu: Acvpu | None = None
    on_oxygen: bool | None = None
    pain: int | None = Field(default=None, ge=0, le=10)
    systolic: float | None = Field(default=None, ge=SBP_RANGE[0], le=SBP_RANGE[1])
    diastolic: float | None = Field(default=None, ge=DBP_RANGE[0], le=DBP_RANGE[1])

    @model_validator(mode="after")
    def _bp(self):
        if "systolic" in self.model_fields_set or "diastolic" in self.model_fields_set:
            _check_bp(self.systolic, self.diastolic)
        return self


class ManualVitals(BaseModel):
    """A field sent as null clears that manual value; a missing field is left alone."""

    hr: float | None = Field(default=None, ge=HR_RANGE[0], le=HR_RANGE[1])
    spo2: float | None = Field(default=None, ge=SPO2_RANGE[0], le=SPO2_RANGE[1])
    rr: float | None = Field(default=None, ge=RR_RANGE[0], le=RR_RANGE[1])
    temp: float | None = Field(default=None, ge=TEMP_RANGE[0], le=TEMP_RANGE[1])


class Unmeasured(BaseModel):
    """A required value that cannot be measured, with the reason. reason=None withdraws it."""

    param: Literal["hr", "spo2", "rr", "temp", "systolic"]
    reason: str | None = Field(default=None, min_length=3, max_length=200)


class SensorModeChange(BaseModel):
    mode: Literal["SERIAL", "MOCK"]
    scenario: Literal["stable", "deteriorating"] | None = None


class NoteCreate(BaseModel):
    text: str = Field(min_length=1, max_length=500)
