"""Authentication state and normalized MESH domain data."""

from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, SecretStr


class AuthState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_id: SecretStr
    client_secret: SecretStr
    refresh_token: SecretStr | None = None
    mesh_access_token: SecretStr | None = None
    mos_expires_in: int | None = None
    mesh_expires_at: int | None = None
    refreshed_at: str | None = None
    refresh_via: str | None = None
    activated: bool = False


class RegistrationMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bearer: SecretStr
    software_statement: SecretStr
    source: str = Field(min_length=1)


class Student(BaseModel):
    id: str
    profile_id: str
    person_id: str | None = None
    name: str


class Mark(BaseModel):
    id: str
    student_id: str
    subject_id: str
    subject_name: str
    value: str
    numeric_value: Decimal | None = None
    weight: int = 1
    work_type: str = ""
    comment: str = ""
    lesson_date: date
    created_at_mesh: str | None = None


class Homework(BaseModel):
    id: str
    student_id: str
    subject_id: str
    subject_name: str
    lesson_date: date
    text: str


class SubjectAverage(BaseModel):
    student_id: str
    subject_id: str
    subject_name: str
    period_id: str
    period_start: date | None = None
    period_end: date | None = None
    average: Decimal | None = None
