from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import signal
import smtplib
import sys
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


def utc_now() -> datetime:
    return datetime.now(UTC)


def to_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None

    normalized = value
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"

    return datetime.fromisoformat(normalized)


def split_full_name(full_name: str) -> tuple[str, str]:
    parts = [part for part in full_name.strip().split() if part]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def normalize_email(email: str) -> str:
    return email.strip().lower()


def password_digest(password: str, salt: str) -> str:
    return hashlib.sha256(f"{salt}:{password}".encode("utf-8")).hexdigest()


def generate_trace_id() -> str:
    return secrets.token_hex(16)


class AppError(Exception):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


@dataclass(slots=True)
class ServiceConfig:
    api_host: str
    api_port: int
    auth_host: str
    auth_port: int
    allowed_email_domain: str
    smtp_host: str
    smtp_port: int
    sender_email: str
    sender_name: str
    otp_expiry_minutes: int
    otp_resend_cooldown_seconds: int
    otp_hourly_request_limit: int
    otp_hourly_window_seconds: int
    otp_maximum_attempts: int
    otp_block_duration_minutes: int
    access_token_ttl_minutes: int
    refresh_token_ttl_minutes: int
    app_version: str


def parse_listen_address(value: str, label: str) -> tuple[str, int]:
    if ":" not in value:
        raise ValueError(f"{label} must use host:port format.")

    host, port_text = value.rsplit(":", 1)
    port = int(port_text)
    return host, port


def load_config(path: Path) -> ServiceConfig:
    data = json.loads(path.read_text(encoding="utf-8"))
    api_host, api_port = parse_listen_address(data["apiListen"], "apiListen")
    auth_host, auth_port = parse_listen_address(data["authListen"], "authListen")

    return ServiceConfig(
        api_host=api_host,
        api_port=api_port,
        auth_host=auth_host,
        auth_port=auth_port,
        allowed_email_domain=data["allowedEmailDomain"].lower(),
        smtp_host=data["smtpHost"],
        smtp_port=int(data["smtpPort"]),
        sender_email=data["senderEmail"],
        sender_name=data["senderName"],
        otp_expiry_minutes=int(data["otpExpiryMinutes"]),
        otp_resend_cooldown_seconds=int(data["otpResendCooldownSeconds"]),
        otp_hourly_request_limit=int(data["otpHourlyRequestLimit"]),
        otp_hourly_window_seconds=int(data["otpHourlyWindowSeconds"]),
        otp_maximum_attempts=int(data["otpMaximumAttempts"]),
        otp_block_duration_minutes=int(data["otpBlockDurationMinutes"]),
        access_token_ttl_minutes=int(data["accessTokenTtlMinutes"]),
        refresh_token_ttl_minutes=int(data["refreshTokenTtlMinutes"]),
        app_version=data.get("appVersion", "local-e2e"),
    )


class StateStore:
    def __init__(self, path: Path, config: ServiceConfig) -> None:
        self._path = path
        self._config = config
        self._lock = threading.RLock()
        self._state = self._load()

    def _default_state(self) -> dict[str, Any]:
        return {
            "meta": {
                "next_user_id": 1,
                "next_auth_id": 1,
                "next_session_id": 1,
            },
            "guests": {},
            "users": {},
            "sessions": {},
            "rate_limits": {
                "register": {},
                "forgot": {},
            },
        }

    def _load(self) -> dict[str, Any]:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if not self._path.exists():
            state = self._default_state()
            self._path.write_text(json.dumps(state, indent=2), encoding="utf-8")
            return state

        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            state = self._default_state()
            self._path.write_text(json.dumps(state, indent=2), encoding="utf-8")
            return state

    def _save(self) -> None:
        self._path.write_text(json.dumps(self._state, indent=2), encoding="utf-8")

    def _next_id(self, key: str) -> int:
        value = int(self._state["meta"][key])
        self._state["meta"][key] = value + 1
        return value

    def _get_or_create_guest(self, browser_id: str) -> dict[str, Any]:
        guests = self._state["guests"]
        guest = guests.get(browser_id)
        if guest is None:
            guest = {
                "guest_user_id": self._next_id("next_user_id"),
                "created_at": to_iso(utc_now()),
            }
            guests[browser_id] = guest
        return guest

    def _profile_payload(self, user: dict[str, Any]) -> dict[str, Any]:
        profile = user["profile"]
        return {
            "first_name": user["first_name"],
            "last_name": user["last_name"],
            "company_name": profile.get("company_name"),
            "occupation": profile.get("occupation"),
            "first_discover": profile.get("first_discover"),
            "enterprise_client_name": None,
            "enterprise_client_id": None,
            "is_enterprise_first_login": False,
            "tos_version": profile.get("tos_version"),
            "pp_version": profile.get("pp_version"),
            "is_profile_complete": bool(profile.get("is_profile_complete")),
        }

    def _login_profile_payload(
        self,
        *,
        user_id: int,
        auth_id: int,
        email: str | None,
        first_name: str | None,
        last_name: str | None,
        is_email_verified: bool,
        guest_user_id: int | None = None,
    ) -> dict[str, Any]:
        return {
            "id": auth_id,
            "email": email,
            "first_name": first_name,
            "last_name": last_name,
            "user_id": str(user_id),
            "guest_user_id": guest_user_id,
            "is_email_verified": is_email_verified,
        }

    def _create_session(
        self,
        *,
        kind: str,
        user_id: int,
        auth_id: int,
        email: str | None,
        first_name: str | None,
        last_name: str | None,
        is_email_verified: bool,
        browser_id: str | None,
        guest_user_id: int | None = None,
    ) -> dict[str, Any]:
        session_id = str(self._next_id("next_session_id"))
        access_token = f"at_{secrets.token_urlsafe(24)}"
        refresh_token = f"rt_{secrets.token_urlsafe(24)}"
        now = utc_now()

        session = {
            "id": session_id,
            "kind": kind,
            "user_id": user_id,
            "auth_id": auth_id,
            "guest_user_id": guest_user_id,
            "email": email,
            "first_name": first_name,
            "last_name": last_name,
            "is_email_verified": is_email_verified,
            "browser_id": browser_id,
            "access_token": access_token,
            "refresh_token": refresh_token,
            "access_expires_at": to_iso(now + timedelta(minutes=self._config.access_token_ttl_minutes)),
            "refresh_expires_at": to_iso(now + timedelta(minutes=self._config.refresh_token_ttl_minutes)),
        }
        self._state["sessions"][session_id] = session
        return session

    def _find_session(
        self,
        *,
        access_token: str | None = None,
        refresh_token: str | None = None,
    ) -> dict[str, Any] | None:
        now = utc_now()
        sessions = self._state["sessions"]
        stale_session_ids: list[str] = []

        for session_id, session in sessions.items():
            access_expires_at = parse_iso(session.get("access_expires_at"))
            refresh_expires_at = parse_iso(session.get("refresh_expires_at"))

            if refresh_expires_at and refresh_expires_at <= now:
                stale_session_ids.append(session_id)
                continue

            if access_token and session.get("access_token") == access_token:
                if access_expires_at and access_expires_at <= now:
                    return None
                return session

            if refresh_token and session.get("refresh_token") == refresh_token:
                return session

        for session_id in stale_session_ids:
            sessions.pop(session_id, None)

        if stale_session_ids:
            self._save()

        return None

    def _get_rate_bucket(self, operation: str, email: str) -> dict[str, Any]:
        buckets = self._state["rate_limits"][operation]
        bucket = buckets.get(email)
        if bucket is None:
            bucket = {
                "cooldown_until": None,
                "hourly_window_start": None,
                "hourly_count": 0,
                "hourly_blocked_until": None,
                "attempt_count": 0,
                "attempt_window_start": None,
                "blocked_until": None,
            }
            buckets[email] = bucket
        return bucket

    def _check_cooldown(self, operation: str, email: str) -> None:
        bucket = self._get_rate_bucket(operation, email)
        now = utc_now()
        cooldown_until = parse_iso(bucket.get("cooldown_until"))
        if cooldown_until and cooldown_until > now:
            seconds = max(1, int((cooldown_until - now).total_seconds()))
            raise AppError(
                HTTPStatus.BAD_REQUEST,
                "OTP_COOLDOWN_ACTIVE",
                f"Too many requests. Please wait {seconds} seconds before trying again.",
            )

    def _set_cooldown(self, operation: str, email: str) -> datetime:
        bucket = self._get_rate_bucket(operation, email)
        cooldown_until = utc_now() + timedelta(seconds=self._config.otp_resend_cooldown_seconds)
        bucket["cooldown_until"] = to_iso(cooldown_until)
        return cooldown_until

    def _check_hourly_rate_limit(self, operation: str, email: str) -> None:
        bucket = self._get_rate_bucket(operation, email)
        now = utc_now()
        blocked_until = parse_iso(bucket.get("hourly_blocked_until"))

        if blocked_until and blocked_until > now:
            minutes = max(1, int((blocked_until - now).total_seconds() / 60))
            raise AppError(
                HTTPStatus.BAD_REQUEST,
                "OTP_HOURLY_LIMIT_EXCEEDED",
                f"Too many requests. Please wait {minutes} minute{'s' if minutes != 1 else ''} before trying again.",
            )

        window_start = parse_iso(bucket.get("hourly_window_start"))
        if window_start is None or (now - window_start).total_seconds() >= self._config.otp_hourly_window_seconds:
            bucket["hourly_window_start"] = to_iso(now)
            bucket["hourly_count"] = 0

        bucket["hourly_count"] = int(bucket.get("hourly_count", 0)) + 1

        if bucket["hourly_count"] > self._config.otp_hourly_request_limit:
            blocked_until = now + timedelta(seconds=self._config.otp_hourly_window_seconds)
            bucket["hourly_blocked_until"] = to_iso(blocked_until)
            minutes = max(1, int(self._config.otp_hourly_window_seconds / 60))
            raise AppError(
                HTTPStatus.BAD_REQUEST,
                "OTP_HOURLY_LIMIT_EXCEEDED",
                f"Too many requests. Please wait {minutes} minute{'s' if minutes != 1 else ''} before trying again.",
            )

    def _check_attempt_block(self, operation: str, email: str) -> None:
        bucket = self._get_rate_bucket(operation, email)
        blocked_until = parse_iso(bucket.get("blocked_until"))
        now = utc_now()
        if blocked_until and blocked_until > now:
            minutes = max(1, int((blocked_until - now).total_seconds() / 60))
            raise AppError(
                HTTPStatus.BAD_REQUEST,
                "OTP_BLOCKED",
                (
                    "You have reached the maximum number of OTP request attempts. "
                    f"Please try again after {minutes} minutes."
                ),
            )

    def _record_failed_attempt(self, operation: str, email: str) -> None:
        bucket = self._get_rate_bucket(operation, email)
        now = utc_now()
        window_start = parse_iso(bucket.get("attempt_window_start"))
        block_window = timedelta(minutes=self._config.otp_block_duration_minutes)

        if window_start is None or (now - window_start) >= block_window:
            bucket["attempt_window_start"] = to_iso(now)
            bucket["attempt_count"] = 0

        bucket["attempt_count"] = int(bucket.get("attempt_count", 0)) + 1

        if bucket["attempt_count"] > self._config.otp_maximum_attempts:
            bucket["blocked_until"] = to_iso(now + block_window)

    def _clear_attempts(self, operation: str, email: str) -> None:
        bucket = self._get_rate_bucket(operation, email)
        bucket["attempt_count"] = 0
        bucket["attempt_window_start"] = None
        bucket["blocked_until"] = None

    def guest_login(self, browser_id: str) -> dict[str, Any]:
        if not browser_id.strip():
            raise AppError(HTTPStatus.BAD_REQUEST, "MISSING_REQUEST_HEADER", "Missing browser id.")

        with self._lock:
            guest = self._get_or_create_guest(browser_id)
            session = self._create_session(
                kind="guest",
                user_id=int(guest["guest_user_id"]),
                auth_id=int(guest["guest_user_id"]),
                email=None,
                first_name=None,
                last_name=None,
                is_email_verified=False,
                browser_id=browser_id,
                guest_user_id=int(guest["guest_user_id"]),
            )
            self._save()

        return {
            "access_token": session["access_token"],
            "refresh_token": session["refresh_token"],
            "profile": self._login_profile_payload(
                user_id=int(guest["guest_user_id"]),
                auth_id=int(guest["guest_user_id"]),
                email=None,
                first_name=None,
                last_name=None,
                is_email_verified=False,
                guest_user_id=int(guest["guest_user_id"]),
            ),
        }

    def get_session_from_access_token(self, access_token: str) -> dict[str, Any]:
        with self._lock:
            session = self._find_session(access_token=access_token)
            if session is None:
                raise AppError(HTTPStatus.UNAUTHORIZED, "INVALID_TOKEN", "Unauthorized")
            return session

    def refresh_session(self, refresh_token: str) -> dict[str, Any]:
        with self._lock:
            session = self._find_session(refresh_token=refresh_token)
            if session is None:
                raise AppError(HTTPStatus.UNAUTHORIZED, "INVALID_TOKEN", "Unauthorized")

            session["access_token"] = f"at_{secrets.token_urlsafe(24)}"
            session["refresh_token"] = f"rt_{secrets.token_urlsafe(24)}"
            session["access_expires_at"] = to_iso(utc_now() + timedelta(minutes=self._config.access_token_ttl_minutes))
            session["refresh_expires_at"] = to_iso(utc_now() + timedelta(minutes=self._config.refresh_token_ttl_minutes))
            self._save()

            return {
                "access_token": session["access_token"],
                "refresh_token": session["refresh_token"],
                "profile": self._login_profile_payload(
                    user_id=int(session["user_id"]),
                    auth_id=int(session["auth_id"]),
                    email=session.get("email"),
                    first_name=session.get("first_name"),
                    last_name=session.get("last_name"),
                    is_email_verified=bool(session.get("is_email_verified")),
                    guest_user_id=session.get("guest_user_id"),
                ),
            }

    def check_email_availability(self, email: str) -> dict[str, str]:
        normalized_email = normalize_email(email)

        with self._lock:
            user = self._state["users"].get(normalized_email)
            next_step = "REGISTER" if user is None else "LOGIN"

        return {"next_step": next_step}

    def register_user(
        self,
        *,
        email: str,
        password: str,
        full_name: str,
        tos_version: str,
        pp_version: str,
        browser_id: str,
    ) -> dict[str, Any]:
        normalized_email = normalize_email(email)
        if not browser_id.strip():
            raise AppError(HTTPStatus.BAD_REQUEST, "MISSING_REQUEST_HEADER", "Missing x-browser-id header")

        if not normalized_email.endswith(f"@{self._config.allowed_email_domain}"):
            raise AppError(
                HTTPStatus.BAD_REQUEST,
                "INVALID_EMAIL_FORMAT",
                f"Only @{self._config.allowed_email_domain} email addresses are accepted for local E2E flows.",
            )

        with self._lock:
            self._check_hourly_rate_limit("register", normalized_email)
            self._check_cooldown("register", normalized_email)

            existing_user = self._state["users"].get(normalized_email)
            if existing_user is not None:
                raise AppError(
                    HTTPStatus.BAD_REQUEST,
                    "EMAIL_ALREADY_REGISTERED",
                    "You previously signed up with Email. Please click 'Sign in with Email'",
                )

            first_name, last_name = split_full_name(full_name)
            salt = secrets.token_hex(8)
            auth_id = self._next_id("next_auth_id")
            user_id = self._next_id("next_user_id")
            otp_code = f"{secrets.randbelow(1_000_000):06d}"
            otp_expires_at = utc_now() + timedelta(minutes=self._config.otp_expiry_minutes)
            next_resend_at = self._set_cooldown("register", normalized_email)
            self._get_or_create_guest(browser_id)

            self._state["users"][normalized_email] = {
                "auth_id": auth_id,
                "user_id": user_id,
                "email": normalized_email,
                "password_hash": password_digest(password, salt),
                "password_salt": salt,
                "full_name": full_name.strip(),
                "first_name": first_name,
                "last_name": last_name,
                "is_email_verified": False,
                "created_at": to_iso(utc_now()),
                "register_otp": {
                    "code": otp_code,
                    "expires_at": to_iso(otp_expires_at),
                    "sent_at": to_iso(utc_now()),
                },
                "forgot_password_otp": None,
                "profile": {
                    "company_name": None,
                    "occupation": None,
                    "first_discover": None,
                    "is_profile_complete": False,
                    "tos_version": tos_version,
                    "pp_version": pp_version,
                },
            }
            self._save()

        return {
            "otp_code": otp_code,
            "otp_expires_at": otp_expires_at,
            "next_resend_at": next_resend_at,
            "full_name": full_name.strip(),
        }

    def resend_registration_otp(self, email: str) -> dict[str, Any]:
        normalized_email = normalize_email(email)

        with self._lock:
            user = self._state["users"].get(normalized_email)
            if user is None:
                raise AppError(HTTPStatus.BAD_REQUEST, "BAD_REQUEST", "Account was not found.")

            if user.get("is_email_verified"):
                raise AppError(HTTPStatus.BAD_REQUEST, "BAD_REQUEST", "Email is already verified.")

            self._check_hourly_rate_limit("register", normalized_email)
            self._check_attempt_block("register", normalized_email)
            self._check_cooldown("register", normalized_email)

            otp_code = f"{secrets.randbelow(1_000_000):06d}"
            otp_expires_at = utc_now() + timedelta(minutes=self._config.otp_expiry_minutes)
            next_resend_at = self._set_cooldown("register", normalized_email)
            user["register_otp"] = {
                "code": otp_code,
                "expires_at": to_iso(otp_expires_at),
                "sent_at": to_iso(utc_now()),
            }
            self._save()

            return {
                "otp_code": otp_code,
                "otp_expires_at": otp_expires_at,
                "next_resend_at": next_resend_at,
                "full_name": user["full_name"],
            }

    def verify_registration_otp(
        self,
        *,
        email: str,
        otp_code: str,
        session: dict[str, Any],
    ) -> dict[str, Any]:
        normalized_email = normalize_email(email)

        with self._lock:
            self._check_attempt_block("register", normalized_email)
            user = self._state["users"].get(normalized_email)
            if user is None or user.get("register_otp") is None:
                self._record_failed_attempt("register", normalized_email)
                self._save()
                raise AppError(
                    HTTPStatus.BAD_REQUEST,
                    "BAD_REQUEST",
                    "The code is incorrect. Please check your email and try again.",
                )

            active_otp = user["register_otp"]
            expires_at = parse_iso(active_otp.get("expires_at"))
            if expires_at is None or expires_at <= utc_now() or active_otp.get("code") != otp_code:
                self._record_failed_attempt("register", normalized_email)
                self._save()
                raise AppError(
                    HTTPStatus.BAD_REQUEST,
                    "BAD_REQUEST",
                    "The code is incorrect. Please check your email and try again.",
                )

            self._clear_attempts("register", normalized_email)
            user["is_email_verified"] = True
            user["register_otp"] = None

            auth_session = self._create_session(
                kind="auth",
                user_id=int(user["user_id"]),
                auth_id=int(user["auth_id"]),
                email=user["email"],
                first_name=user["first_name"],
                last_name=user["last_name"],
                is_email_verified=True,
                browser_id=session.get("browser_id"),
                guest_user_id=session.get("guest_user_id"),
            )
            self._save()

            return {
                "is_success": True,
                "access_token": auth_session["access_token"],
                "refresh_token": auth_session["refresh_token"],
                "profile": self._login_profile_payload(
                    user_id=int(user["user_id"]),
                    auth_id=int(user["auth_id"]),
                    email=user["email"],
                    first_name=user["first_name"],
                    last_name=user["last_name"],
                    is_email_verified=True,
                    guest_user_id=session.get("guest_user_id"),
                ),
            }

    def login_with_email(
        self,
        *,
        email: str,
        password: str,
        session: dict[str, Any] | None,
        browser_id: str | None,
    ) -> dict[str, Any]:
        normalized_email = normalize_email(email)

        with self._lock:
            user = self._state["users"].get(normalized_email)
            if user is None:
                raise AppError(HTTPStatus.BAD_REQUEST, "BAD_REQUEST", "Invalid email or password")

            expected_hash = password_digest(password, user["password_salt"])
            if expected_hash != user["password_hash"]:
                raise AppError(HTTPStatus.BAD_REQUEST, "BAD_REQUEST", "Invalid email or password")

            if not user["is_email_verified"]:
                register_otp = user.get("register_otp")
                expires_at = parse_iso(register_otp.get("expires_at")) if register_otp else None
                if register_otp is None or expires_at is None or expires_at <= utc_now():
                    otp_code = f"{secrets.randbelow(1_000_000):06d}"
                    user["register_otp"] = {
                        "code": otp_code,
                        "expires_at": to_iso(utc_now() + timedelta(minutes=self._config.otp_expiry_minutes)),
                        "sent_at": to_iso(utc_now()),
                    }
                    self._save()
                    return {
                        "error": AppError(
                            HTTPStatus.UNAUTHORIZED,
                            "EMAIL_NOT_VERIFIED",
                            "Please verify your email address.",
                        ),
                        "otp_code": otp_code,
                        "full_name": user["full_name"],
                    }

                raise AppError(
                    HTTPStatus.UNAUTHORIZED,
                    "EMAIL_NOT_VERIFIED",
                    "Please verify your email address.",
                )

            guest_user_id = session.get("guest_user_id") if session else None
            auth_session = self._create_session(
                kind="auth",
                user_id=int(user["user_id"]),
                auth_id=int(user["auth_id"]),
                email=user["email"],
                first_name=user["first_name"],
                last_name=user["last_name"],
                is_email_verified=True,
                browser_id=browser_id or (session.get("browser_id") if session else None),
                guest_user_id=guest_user_id,
            )
            self._save()

            return {
                "access_token": auth_session["access_token"],
                "refresh_token": auth_session["refresh_token"],
                "profile": self._login_profile_payload(
                    user_id=int(user["user_id"]),
                    auth_id=int(user["auth_id"]),
                    email=user["email"],
                    first_name=user["first_name"],
                    last_name=user["last_name"],
                    is_email_verified=True,
                    guest_user_id=guest_user_id,
                ),
            }

    def get_profile(self, session: dict[str, Any]) -> dict[str, Any]:
        email = session.get("email")
        if not email:
            raise AppError(HTTPStatus.UNAUTHORIZED, "UNAUTHORIZED", "Unauthorized")

        with self._lock:
            user = self._state["users"].get(normalize_email(email))
            if user is None:
                raise AppError(HTTPStatus.UNAUTHORIZED, "UNAUTHORIZED", "Unauthorized")
            return self._profile_payload(user)

    def update_profile(self, session: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        email = session.get("email")
        if not email:
            raise AppError(HTTPStatus.UNAUTHORIZED, "UNAUTHORIZED", "Unauthorized")

        with self._lock:
            user = self._state["users"].get(normalize_email(email))
            if user is None:
                raise AppError(HTTPStatus.UNAUTHORIZED, "UNAUTHORIZED", "Unauthorized")

            profile = user["profile"]
            if isinstance(payload.get("company_name"), str):
                profile["company_name"] = payload["company_name"].strip()
            if isinstance(payload.get("occupation"), str):
                profile["occupation"] = payload["occupation"].strip()
            if isinstance(payload.get("first_discover"), str):
                profile["first_discover"] = payload["first_discover"].strip()
            if isinstance(payload.get("tos_version"), str):
                profile["tos_version"] = payload["tos_version"].strip()
            if isinstance(payload.get("pp_version"), str):
                profile["pp_version"] = payload["pp_version"].strip()
            if isinstance(payload.get("full_name"), str):
                full_name = payload["full_name"].strip()
                first_name, last_name = split_full_name(full_name)
                user["full_name"] = full_name
                user["first_name"] = first_name
                user["last_name"] = last_name

            profile["is_profile_complete"] = bool(
                profile.get("company_name") and profile.get("occupation") and profile.get("first_discover")
            )
            self._save()

            session["first_name"] = user["first_name"]
            session["last_name"] = user["last_name"]
            return self._profile_payload(user)

    def forgot_password(self, email: str) -> dict[str, Any]:
        normalized_email = normalize_email(email)

        with self._lock:
            user = self._state["users"].get(normalized_email)
            if user is None:
                raise AppError(HTTPStatus.BAD_REQUEST, "BAD_REQUEST", "Account was not found.")
            if not user.get("is_email_verified"):
                raise AppError(
                    HTTPStatus.BAD_REQUEST,
                    "BAD_REQUEST",
                    "Please verify your email before resetting the password.",
                )

            self._check_hourly_rate_limit("forgot", normalized_email)
            self._check_cooldown("forgot", normalized_email)

            otp_code = f"{secrets.randbelow(1_000_000):06d}"
            otp_expires_at = utc_now() + timedelta(minutes=self._config.otp_expiry_minutes)
            next_resend_at = self._set_cooldown("forgot", normalized_email)
            user["forgot_password_otp"] = {
                "code": otp_code,
                "expires_at": to_iso(otp_expires_at),
                "sent_at": to_iso(utc_now()),
                "verified_at": None,
            }
            self._save()

            return {
                "otp_code": otp_code,
                "otp_expires_at": otp_expires_at,
                "next_resend_at": next_resend_at,
                "full_name": user["full_name"],
            }

    def verify_forgot_password(self, *, email: str, otp_code: str) -> dict[str, Any]:
        normalized_email = normalize_email(email)

        with self._lock:
            self._check_attempt_block("forgot", normalized_email)
            user = self._state["users"].get(normalized_email)
            otp_record = None if user is None else user.get("forgot_password_otp")
            expires_at = parse_iso(otp_record.get("expires_at")) if otp_record else None

            if otp_record is None or expires_at is None or expires_at <= utc_now() or otp_record.get("code") != otp_code:
                self._record_failed_attempt("forgot", normalized_email)
                self._save()
                raise AppError(
                    HTTPStatus.BAD_REQUEST,
                    "BAD_REQUEST",
                    "The code is incorrect. Please check your email and try again.",
                )

            otp_record["verified_at"] = to_iso(utc_now())
            self._clear_attempts("forgot", normalized_email)
            self._save()

            return {"is_success": True}

    def update_password(self, *, email: str, otp_code: str, new_password: str) -> dict[str, Any]:
        normalized_email = normalize_email(email)

        with self._lock:
            user = self._state["users"].get(normalized_email)
            if user is None or user.get("forgot_password_otp") is None:
                raise AppError(HTTPStatus.BAD_REQUEST, "BAD_REQUEST", "Password reset session was not found.")

            otp_record = user["forgot_password_otp"]
            expires_at = parse_iso(otp_record.get("expires_at"))
            verified_at = parse_iso(otp_record.get("verified_at"))
            if (
                otp_record.get("code") != otp_code
                or expires_at is None
                or expires_at <= utc_now()
                or verified_at is None
            ):
                raise AppError(HTTPStatus.BAD_REQUEST, "BAD_REQUEST", "Password reset session was not found.")

            salt = secrets.token_hex(8)
            user["password_salt"] = salt
            user["password_hash"] = password_digest(new_password, salt)
            user["forgot_password_otp"] = None
            self._save()

            return {"is_success": True}


class MailpitMailer:
    def __init__(self, config: ServiceConfig) -> None:
        self._config = config

    def send_register_otp(self, *, recipient: str, full_name: str, otp_code: str) -> None:
        self._send_email(
            recipient=recipient,
            subject="Verify your Asklumia account",
            html_body=self._build_html(
                greeting_name=full_name,
                intro="Use the verification code below to activate your Asklumia account.",
                otp_code=otp_code,
                closing=f"This code expires in {self._config.otp_expiry_minutes} minutes.",
            ),
            text_body=(
                f"Hello {full_name},\n\n"
                f"Use this verification code to activate your Asklumia account: {otp_code}\n\n"
                f"This code expires in {self._config.otp_expiry_minutes} minutes.\n"
            ),
        )

    def send_forgot_password_otp(self, *, recipient: str, full_name: str, otp_code: str) -> None:
        self._send_email(
            recipient=recipient,
            subject="Reset your Asklumia password",
            html_body=self._build_html(
                greeting_name=full_name,
                intro="Use the reset code below to continue updating your password.",
                otp_code=otp_code,
                closing=f"This code expires in {self._config.otp_expiry_minutes} minutes.",
            ),
            text_body=(
                f"Hello {full_name},\n\n"
                f"Use this reset code to continue updating your password: {otp_code}\n\n"
                f"This code expires in {self._config.otp_expiry_minutes} minutes.\n"
            ),
        )

    def _build_html(self, *, greeting_name: str, intro: str, otp_code: str, closing: str) -> str:
        return f"""\
<!doctype html>
<html>
  <body style="font-family: Arial, sans-serif; color: #111827; line-height: 1.5; padding: 24px;">
    <p>Hello {greeting_name},</p>
    <p>{intro}</p>
    <div class="code" style="font-size: 32px; font-weight: bold; letter-spacing: 8px; margin: 24px 0;">
      {otp_code}
    </div>
    <p>{closing}</p>
  </body>
</html>
"""

    def _send_email(self, *, recipient: str, subject: str, html_body: str, text_body: str) -> None:
        message = EmailMessage()
        message["From"] = f"{self._config.sender_name} <{self._config.sender_email}>"
        message["To"] = recipient
        message["Subject"] = subject
        message.set_content(text_body)
        message.add_alternative(html_body, subtype="html")

        with smtplib.SMTP(self._config.smtp_host, self._config.smtp_port, timeout=10) as client:
            client.send_message(message)


class LocalBackendApp:
    def __init__(self, config: ServiceConfig, state_path: Path) -> None:
        self.config = config
        self.state_store = StateStore(state_path, config)
        self.mailer = MailpitMailer(config)

    def api_health(self) -> dict[str, Any]:
        return {"status": "healthy", "service": "asklumia-lite-api"}

    def auth_health(self) -> dict[str, Any]:
        return {"status": "healthy", "service": "asklumia-lite-auth"}


class AppHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address: tuple[str, int], handler_class: type[BaseHTTPRequestHandler], app: LocalBackendApp):
        self.app = app
        super().__init__(server_address, handler_class)


class JsonHandler(BaseHTTPRequestHandler):
    server_version = "AsklumiaLite/1.0"
    error_content_type = "application/json"

    def log_message(self, format_string: str, *args: Any) -> None:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{timestamp}] {self.address_string()} {format_string % args}")

    def _app(self) -> LocalBackendApp:
        return self.server.app  # type: ignore[attr-defined]

    def _send_json(self, status_code: int, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(status_code)
        self._send_cors_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _send_cors_headers(self) -> None:
        origin = self.headers.get("Origin", "*")
        self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PATCH, OPTIONS")
        self.send_header(
            "Access-Control-Allow-Headers",
            "Authorization, Content-Type, x-browser-id, X-BROWSER-ID, X-POSTHOG-SESSION-ID, X-CLIENT-ID, X-GA-SESSION-ID",
        )

    def _success(self, result: dict[str, Any], *, status_code: int = HTTPStatus.OK) -> None:
        payload = {
            "trace_id": generate_trace_id(),
            "version": self._app().config.app_version,
            "status": "success",
            "result": result,
        }
        self._send_json(status_code, payload)

    def _error(self, error: AppError) -> None:
        payload = {
            "trace_id": generate_trace_id(),
            "version": self._app().config.app_version,
            "status": "fail",
            "message": error.message,
            "result": None,
            "errors": [
                {
                    "error": error.message,
                    "code": error.code,
                }
            ],
        }
        self._send_json(error.status_code, payload)

    def _read_json_body(self) -> dict[str, Any]:
        content_length = int(self.headers.get("Content-Length", "0") or 0)
        raw_body = self.rfile.read(content_length) if content_length > 0 else b"{}"
        try:
            body = json.loads(raw_body.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise AppError(HTTPStatus.BAD_REQUEST, "BAD_REQUEST", "Invalid JSON body.") from exc

        if not isinstance(body, dict):
            raise AppError(HTTPStatus.BAD_REQUEST, "BAD_REQUEST", "Invalid JSON body.")

        return body

    def _access_token(self) -> str:
        authorization = self.headers.get("Authorization", "")
        if not authorization.startswith("Bearer "):
            raise AppError(HTTPStatus.UNAUTHORIZED, "UNAUTHORIZED", "Unauthorized")
        return authorization[7:].strip()

    def _browser_id_from_headers(self) -> str | None:
        return self.headers.get("x-browser-id") or self.headers.get("X-BROWSER-ID")

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(HTTPStatus.NO_CONTENT)
        self._send_cors_headers()
        self.send_header("Content-Length", "0")
        self.end_headers()


class ApiHandler(JsonHandler):
    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            if path == "/health":
                self._success(self._app().api_health())
                return

            if path == "/auth/profile":
                session = self._app().state_store.get_session_from_access_token(self._access_token())
                self._success(self._app().state_store.get_profile(session))
                return

            raise AppError(HTTPStatus.NOT_FOUND, "NOT_FOUND", "Route was not found.")
        except AppError as error:
            self._error(error)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            body = self._read_json_body()
            if path == "/auth/guest":
                browser_id = str(body.get("browser_id") or self._browser_id_from_headers() or "").strip()
                self._success(self._app().state_store.guest_login(browser_id))
                return

            if path == "/auth/email/available":
                self._success(
                    self._app().state_store.check_email_availability(str(body.get("email", ""))),
                )
                return

            if path == "/auth/email/register":
                register_result = self._app().state_store.register_user(
                    email=str(body.get("email", "")),
                    password=str(body.get("password", "")),
                    full_name=str(body.get("full_name", "")),
                    tos_version=str(body.get("tos_version", "")),
                    pp_version=str(body.get("pp_version", "")),
                    browser_id=str(self._browser_id_from_headers() or body.get("browser_id") or "").strip(),
                )
                self._app().mailer.send_register_otp(
                    recipient=str(body.get("email", "")),
                    full_name=register_result["full_name"],
                    otp_code=register_result["otp_code"],
                )
                self._success(
                    {
                        "is_success": True,
                        "otp_expires_at": to_iso(register_result["otp_expires_at"]),
                        "next_resend_at": to_iso(register_result["next_resend_at"]),
                    }
                )
                return

            if path == "/auth/email/register/verify":
                session = self._app().state_store.get_session_from_access_token(self._access_token())
                verify_result = self._app().state_store.verify_registration_otp(
                    email=str(body.get("email", "")),
                    otp_code=str(body.get("otp_code", "")),
                    session=session,
                )
                self._success(verify_result)
                return

            if path == "/auth/email/register/resend":
                resend_result = self._app().state_store.resend_registration_otp(str(body.get("email", "")))
                self._app().mailer.send_register_otp(
                    recipient=str(body.get("email", "")),
                    full_name=resend_result["full_name"],
                    otp_code=resend_result["otp_code"],
                )
                self._success(
                    {
                        "is_success": True,
                        "otp_expires_at": to_iso(resend_result["otp_expires_at"]),
                        "next_resend_at": to_iso(resend_result["next_resend_at"]),
                    }
                )
                return

            if path == "/auth/email/login":
                session = None
                authorization = self.headers.get("Authorization", "")
                if authorization.startswith("Bearer "):
                    try:
                        session = self._app().state_store.get_session_from_access_token(self._access_token())
                    except AppError:
                        session = None

                login_result = self._app().state_store.login_with_email(
                    email=str(body.get("email", "")),
                    password=str(body.get("password", "")),
                    session=session,
                    browser_id=self._browser_id_from_headers(),
                )
                if "error" in login_result:
                    self._app().mailer.send_register_otp(
                        recipient=str(body.get("email", "")),
                        full_name=login_result["full_name"],
                        otp_code=login_result["otp_code"],
                    )
                    raise login_result["error"]
                self._success(login_result)
                return

            if path == "/auth/email/forgot":
                forgot_result = self._app().state_store.forgot_password(str(body.get("email", "")))
                self._app().mailer.send_forgot_password_otp(
                    recipient=str(body.get("email", "")),
                    full_name=forgot_result["full_name"],
                    otp_code=forgot_result["otp_code"],
                )
                self._success(
                    {
                        "is_success": True,
                        "otp_expires_at": to_iso(forgot_result["otp_expires_at"]),
                        "next_resend_at": to_iso(forgot_result["next_resend_at"]),
                    }
                )
                return

            if path == "/auth/email/forgot/verify":
                self._success(
                    self._app().state_store.verify_forgot_password(
                        email=str(body.get("email", "")),
                        otp_code=str(body.get("otp_code", "")),
                    )
                )
                return

            if path == "/auth/email/forgot/update":
                self._success(
                    self._app().state_store.update_password(
                        email=str(body.get("email", "")),
                        otp_code=str(body.get("otp_code", "")),
                        new_password=str(body.get("new_password", "")),
                    )
                )
                return

            if path in {"/auth/google/exchange", "/auth/microsoft", "/auth/sso/check"}:
                raise AppError(
                    HTTPStatus.NOT_IMPLEMENTED,
                    "NOT_IMPLEMENTED",
                    "SSO flows are not supported by the local E2E backend.",
                )

            raise AppError(HTTPStatus.NOT_FOUND, "NOT_FOUND", "Route was not found.")
        except AppError as error:
            self._error(error)
        except smtplib.SMTPException:
            self._error(
                AppError(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    "SMTP_ERROR",
                    "Failed to deliver email to Mailpit.",
                )
            )

    def do_PATCH(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            if path != "/auth/profile":
                raise AppError(HTTPStatus.NOT_FOUND, "NOT_FOUND", "Route was not found.")

            session = self._app().state_store.get_session_from_access_token(self._access_token())
            body = self._read_json_body()
            self._success(self._app().state_store.update_profile(session, body))
        except AppError as error:
            self._error(error)


class AuthHandler(JsonHandler):
    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            if path == "/health":
                self._success(self._app().auth_health())
                return

            raise AppError(HTTPStatus.NOT_FOUND, "NOT_FOUND", "Route was not found.")
        except AppError as error:
            self._error(error)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            body = self._read_json_body()
            if path == "/auth/refresh":
                self._success(self._app().state_store.refresh_session(str(body.get("refresh_token", ""))))
                return

            raise AppError(HTTPStatus.NOT_FOUND, "NOT_FOUND", "Route was not found.")
        except AppError as error:
            self._error(error)


def run_server(config_path: Path, state_path: Path) -> None:
    config = load_config(config_path)
    app = LocalBackendApp(config, state_path)

    api_server = AppHTTPServer((config.api_host, config.api_port), ApiHandler, app)
    auth_server = AppHTTPServer((config.auth_host, config.auth_port), AuthHandler, app)
    stop_event = threading.Event()

    def serve(server: ThreadingHTTPServer, label: str) -> None:
        print(f"{label} listening on {server.server_address[0]}:{server.server_address[1]}")
        server.serve_forever(poll_interval=0.5)

    api_thread = threading.Thread(target=serve, args=(api_server, "api"), daemon=True)
    auth_thread = threading.Thread(target=serve, args=(auth_server, "auth"), daemon=True)
    api_thread.start()
    auth_thread.start()

    def shutdown(*_args: Any) -> None:
        stop_event.set()
        api_server.shutdown()
        auth_server.shutdown()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    try:
        while not stop_event.is_set():
            time.sleep(0.5)
    finally:
        api_server.server_close()
        auth_server.server_close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the local AskLumia E2E backend.")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--state-file", required=True, type=Path)
    args = parser.parse_args()

    run_server(args.config.resolve(), args.state_file.resolve())
    return 0


if __name__ == "__main__":
    sys.exit(main())
