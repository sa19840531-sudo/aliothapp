"""根据可见字段要求生成有界测试资料；验证码和手机号仍需已有资料。"""

import secrets
import string
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def default_test_username(seed):
    """由本次支线标识生成短账号；可见字段要求仍可覆盖默认长度。"""
    token = "".join(char for char in seed.lower() if char in string.hexdigits.lower())
    if len(token) < 11:
        raise ValueError("WEB_ACCOUNT_USERNAME_SEED_INVALID")
    return "w" + token[:11]


class AccountFieldRequirements(BaseModel):
    model_config = ConfigDict(extra="forbid")
    min_length: int | None = Field(default=None, ge=1, le=128)
    max_length: int | None = Field(default=None, ge=1, le=128)
    alphabet: Literal["unrestricted", "alphanumeric", "letters", "digits"] = "unrestricted"
    require_upper: bool = False
    require_lower: bool = False
    require_digit: bool = False
    require_special: bool = False

    @model_validator(mode="after")
    def consistent(self):
        if (self.min_length or 1) > (self.max_length or 128):
            raise ValueError("WEB_ACCOUNT_FIELD_LENGTH_CONFLICT")
        if (
            self.alphabet == "digits"
            and (self.require_upper or self.require_lower)
            or self.alphabet == "letters"
            and self.require_digit
            or self.alphabet != "unrestricted"
            and self.require_special
        ):
            raise ValueError("WEB_ACCOUNT_FIELD_ALPHABET_CONFLICT")
        if sum(
            (self.require_upper, self.require_lower, self.require_digit, self.require_special)
        ) > (self.max_length or 128):
            raise ValueError("WEB_ACCOUNT_FIELD_LENGTH_CONFLICT")
        return self

    @classmethod
    def combine(cls, requirements):
        items = [item for item in requirements if item is not None]
        if not items:
            return None
        alphabets = {item.alphabet for item in items} - {"unrestricted"}
        if {"digits", "letters"} <= alphabets:
            raise ValueError("WEB_ACCOUNT_FIELD_ALPHABET_CONFLICT")
        alphabet = next(
            (kind for kind in ("digits", "letters", "alphanumeric") if kind in alphabets),
            "unrestricted",
        )
        return cls(
            min_length=max(item.min_length or 1 for item in items),
            max_length=min(item.max_length or 128 for item in items),
            alphabet=alphabet,
            **{
                name: any(getattr(item, name) for item in items)
                for name in ("require_upper", "require_lower", "require_digit", "require_special")
            },
        )

    def accepts(self, value):
        if not (self.min_length or 1) <= len(value) <= (self.max_length or 128):
            return False
        alphabets = {
            "alphanumeric": string.ascii_letters + string.digits,
            "letters": string.ascii_letters,
            "digits": string.digits,
        }
        if self.alphabet in alphabets and any(c not in alphabets[self.alphabet] for c in value):
            return False
        return all(
            not required or any(c in group for c in value)
            for required, group in (
                (self.require_upper, string.ascii_uppercase),
                (self.require_lower, string.ascii_lowercase),
                (self.require_digit, string.digits),
                (self.require_special, string.punctuation),
            )
        )

    def generate(self, kind):
        groups = [
            group
            for required, group in (
                (self.require_upper, string.ascii_uppercase),
                (self.require_lower, string.ascii_lowercase),
                (self.require_digit, string.digits),
                (self.require_special, "!@#_-"),
            )
            if required
        ]
        alphabet = {
            "unrestricted": string.ascii_letters + string.digits,
            "alphanumeric": string.ascii_letters + string.digits,
            "letters": string.ascii_letters,
            "digits": string.digits,
        }[self.alphabet]
        length = min(
            self.max_length or 128,
            max(self.min_length or 1, len(groups), 16 if kind == "password" else 12),
        )
        chars = [secrets.choice(group) for group in groups]
        chars.extend(secrets.choice(alphabet) for _ in range(length - len(chars)))
        secrets.SystemRandom().shuffle(chars)
        value = "".join(chars)
        if not self.accepts(value):
            raise ValueError("WEB_ACCOUNT_PROFILE_CONSTRAINT_CONFLICT")
        return value


def generate_test_email(requirements):
    """生成唯一的合成注册邮箱；邮箱本身不证明收件或验证能力。"""
    suffix = "@hot.com"
    if requirements.alphabet != "unrestricted":
        raise ValueError("WEB_ACCOUNT_PROFILE_CONSTRAINT_CONFLICT")
    length = min(requirements.max_length or 128, max(requirements.min_length or 1, 28))
    local_length = length - len(suffix)
    if not 1 <= local_length <= 64:
        raise ValueError("WEB_ACCOUNT_PROFILE_CONSTRAINT_CONFLICT")
    chars = [secrets.choice(string.ascii_lowercase)]
    if requirements.require_upper:
        chars.append(secrets.choice(string.ascii_uppercase))
    if requirements.require_digit:
        chars.append(secrets.choice(string.digits))
    if len(chars) > local_length:
        raise ValueError("WEB_ACCOUNT_PROFILE_CONSTRAINT_CONFLICT")
    tail = chars[1:]
    tail.extend(
        secrets.choice(string.ascii_lowercase + string.digits)
        for _ in range(local_length - len(chars))
    )
    secrets.SystemRandom().shuffle(tail)
    value = chars[0] + "".join(tail) + suffix
    if not requirements.accepts(value):
        raise ValueError("WEB_ACCOUNT_PROFILE_CONSTRAINT_CONFLICT")
    return value


def resolve_profile_value(credentials, kind, requirements=None, *, allow_generate=False):
    kind = "password" if kind == "confirm_password" else kind
    if kind not in {"username", "nickname", "password", "phone", "email", "verification_code"}:
        raise ValueError("WEB_ACCOUNT_FIELD_UNSUPPORTED")
    requirements = AccountFieldRequirements.model_validate(requirements or {})
    resolved = getattr(credentials, "_resolved_values", {})
    value = getattr(credentials, kind, None)
    if kind == "nickname" and not value:
        value = credentials.username
    if not value and kind not in {"username", "nickname", "password", "email"}:
        raise ValueError("WEB_ACCOUNT_MISSING_" + kind.upper())
    if kind == "email" and not value and not allow_generate:
        raise ValueError("WEB_ACCOUNT_MISSING_EMAIL")
    if not value or not requirements.accepts(value):
        if kind in resolved or not allow_generate:
            raise ValueError("WEB_ACCOUNT_PROFILE_CONSTRAINT_CONFLICT")
        if kind == "email":
            value = generate_test_email(requirements)
        elif kind in {"username", "nickname", "password"}:
            value = requirements.generate(kind)
        else:
            raise ValueError("WEB_ACCOUNT_PROFILE_CONSTRAINT_CONFLICT")
    setattr(credentials, kind, value)
    resolved[kind] = value
    credentials._resolved_values = resolved
    return value
