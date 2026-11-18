# -*- coding: utf-8 -*-
"""Blue Dart API client.

Safety rules:
- Waybill creation is NEVER retried after an ambiguous failure (timeout, 5xx, dropped connection).
  Blue Dart may have created the waybill, so a retry could duplicate it. We raise CourierOutcomeUnknown.
- Cancel and login are retried a few times, because repeating them is harmless.
- A 401 refreshes the token once and retries once. A 401 means nothing was processed.
- Logs are redacted before they leave this module.

No Frappe imports: transport, clock, sleep and log are injected.
"""

import base64
import binascii
import json
import time

from izi.izi.courier.bluedart.errors import describe
from izi.izi.courier.bluedart.models import BlueDartConfig, WaybillResult  # noqa: F401 (re-exported)
from izi.izi.courier.bluedart.payload import build_cancel_payload, build_waybill_payload
from izi.izi.courier.bluedart.transport import TransportError, TransportTimeout
from izi.izi.courier.bluedart.validation import validate_shipment
from izi.izi.courier.exceptions import (
    CourierAuthError,
    CourierBusinessError,
    CourierOutcomeUnknown,
    CourierTechnicalError,
    CourierValidationError,
)

TOKEN_PATH = "/token/v1/login"
GENERATE_PATH = "/waybill/v1/GenerateWayBill"
CANCEL_PATH = "/waybill/v1/CancelWaybill"

TOKEN_REFRESH_AFTER_SECONDS = 23 * 3600  # Blue Dart tokens are valid for 24 hours
MAX_ATTEMPTS = 3
BACKOFF_SECONDS = (1, 2)
LOG_TEXT_LIMIT = 2000

TOKEN_KEYS = {"jwttoken", "token", "jwt", "accesstoken", "access_token"}
SECRET_KEYS = {"licencekey", "loginid", "clientid", "clientsecret", "jwttoken", "token", "password", "authorization"}
BINARY_KEYS = {"awbprintcontent", "mplabelprintcontent", "mpslabelprintcontent"}

UNKNOWN_OUTCOME_MESSAGE = (
    "Blue Dart did not confirm waybill creation for reference {ref}. A waybill may already exist. "
    "Do not retry yet: check the Blue Dart portal for reference {ref} first, so no duplicate is created."
)


class BlueDartClient:
    def __init__(self, config, transport, *, clock=time.time, sleep=time.sleep, log=None):
        self.cfg = config
        self.transport = transport
        self.clock = clock
        self.sleep = sleep
        self.log = log or (lambda entry: None)
        self._token = None
        self._token_at = 0.0

    # ------------------------------------------------------------------ public

    def authenticate(self):
        """Fetch a fresh token. Used by the settings 'Test connection' button."""
        self._token_value(force=True)
        return True

    def generate_waybill(self, shipment):
        problems = validate_shipment(shipment)
        if problems:
            raise CourierValidationError(" ".join(problems))

        body = build_waybill_payload(shipment, self.cfg)
        data = self._post(
            "generate_waybill", GENERATE_PATH, body, idempotent=False, reference=shipment.reference
        )

        awb = str(data.get("AWBNo") or "").strip()
        if not awb:
            raise CourierOutcomeUnknown(
                UNKNOWN_OUTCOME_MESSAGE.format(ref=shipment.reference), payload=data
            )
        return WaybillResult(awb=awb, label_pdf=_decode_binary(data.get("AWBPrintContent")), raw=data)

    def cancel_waybill(self, awb):
        awb = str(awb or "").strip()
        if not awb.isdigit() or len(awb) not in (11, 12):
            raise CourierValidationError("Waybill number must be 11 or 12 digits.")
        body = build_cancel_payload(awb, self.cfg)
        return self._post("cancel_waybill", CANCEL_PATH, body, idempotent=True, reference=awb)

    # ----------------------------------------------------------------- tokens

    def _token_value(self, force=False):
        fresh = self._token is not None and (self.clock() - self._token_at) < TOKEN_REFRESH_AFTER_SECONDS
        if force or not fresh:
            return self._login()
        return self._token

    def _login(self):
        url = self.cfg.base_url + TOKEN_PATH
        headers = {"ClientID": self.cfg.client_id, "clientSecret": self.cfg.client_secret}
        started = time.monotonic()
        try:
            resp = self.transport.request("GET", url, headers=headers, json_body=None, timeout=self.cfg.timeout_seconds)
        except TransportTimeout:
            self._emit("login", "", url, None, None, "", "Timeout", started)
            raise CourierTechnicalError("Blue Dart login timed out. Retry later.")
        except TransportError:
            self._emit("login", "", url, None, None, "", "Technical Error", started)
            raise CourierTechnicalError("Blue Dart login is unreachable. Check the server's outbound network access.")

        outcome = "Success" if resp.status == 200 else ("Auth Error" if resp.status in (401, 403) else "Technical Error")
        self._emit("login", "", url, None, resp.status, "[token response not logged]", outcome, started)

        if resp.status in (401, 403):
            raise CourierAuthError("Blue Dart rejected the client ID or client secret.", http_status=resp.status)
        if resp.status >= 500:
            raise CourierTechnicalError("Blue Dart login is unavailable right now. Retry later.", http_status=resp.status)
        if resp.status != 200:
            raise CourierAuthError(f"Unexpected login response (HTTP {resp.status}).", http_status=resp.status)

        token = find_token(resp.text)
        if not token:
            raise CourierAuthError(
                "Login returned HTTP 200 but no token could be read from the response. "
                "The token response format must be confirmed with Blue Dart."
            )
        self._token = token
        self._token_at = self.clock()
        return token

    # ------------------------------------------------------------------ calls

    def _post(self, operation, path, body, *, idempotent, reference=""):
        url = self.cfg.base_url + path
        max_attempts = MAX_ATTEMPTS if idempotent else 1
        attempt = 0
        auth_retried = False

        while True:
            attempt += 1
            started = time.monotonic()
            headers = {
                "Content-Type": "application/json",
                "accept": "application/json",
                "JWTToken": self._token_value(),
            }
            try:
                resp = self.transport.request("POST", url, headers=headers, json_body=body, timeout=self.cfg.timeout_seconds)
            except (TransportTimeout, TransportError) as exc:
                outcome = "Timeout" if isinstance(exc, TransportTimeout) else "Technical Error"
                self._emit(operation, reference, url, body, None, str(exc), outcome, started)
                if attempt < max_attempts:
                    self._backoff(attempt)
                    continue
                if idempotent:
                    raise CourierTechnicalError("Blue Dart did not answer in time. Retry later.")
                raise CourierOutcomeUnknown(UNKNOWN_OUTCOME_MESSAGE.format(ref=reference or "(none)"))

            data = _json_or_none(resp.text)

            if resp.status == 401:
                self._emit(operation, reference, url, body, resp.status, resp.text, "Auth Error", started)
                if not auth_retried:
                    auth_retried = True
                    self._token = None  # force a fresh login; the request was not processed
                    continue
                raise CourierAuthError(
                    "Blue Dart rejected the token twice. Check the client ID, client secret, LoginID and LicenceKey.",
                    http_status=401,
                    payload=_redacted(data),
                )

            if resp.status >= 500:
                self._emit(operation, reference, url, body, resp.status, resp.text, "Technical Error", started)
                if idempotent:
                    if attempt < max_attempts:
                        self._backoff(attempt)
                        continue
                    raise CourierTechnicalError(f"Blue Dart is unavailable (HTTP {resp.status}). Retry later.", http_status=resp.status)
                raise CourierOutcomeUnknown(UNKNOWN_OUTCOME_MESSAGE.format(ref=reference or "(none)"), http_status=resp.status)

            if isinstance(data, dict) and (data.get("IsError") is True or resp.status == 400):
                self._emit(operation, reference, url, body, resp.status, resp.text, "Business Error", started)
                code, message = describe(data.get("Status") or [])
                raise CourierBusinessError(message, code=code, http_status=resp.status, payload=_redacted(data))

            if resp.status != 200 or not isinstance(data, dict):
                self._emit(operation, reference, url, body, resp.status, resp.text, "Technical Error", started)
                if idempotent:
                    raise CourierTechnicalError(f"Unexpected response from Blue Dart (HTTP {resp.status}).", http_status=resp.status)
                raise CourierOutcomeUnknown(UNKNOWN_OUTCOME_MESSAGE.format(ref=reference or "(none)"), http_status=resp.status)

            self._emit(operation, reference, url, body, resp.status, resp.text, "Success", started)
            return data

    # ---------------------------------------------------------------- helpers

    def _backoff(self, attempt):
        self.sleep(BACKOFF_SECONDS[min(attempt - 1, len(BACKOFF_SECONDS) - 1)])

    def _emit(self, operation, reference, url, request_body, status, response_text, outcome, started):
        entry = {
            "operation": operation,
            "reference": reference,
            "url": url,
            "http_status": status,
            "outcome": outcome,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "request": redact(request_body) if request_body is not None else None,
            "response": _log_response(response_text),
        }
        try:
            self.log(entry)
        except Exception:
            pass  # logging must never change the result of a shipment call; the adapter reports its own failures


def find_token(text):
    """Read the token from the login response. The response shape is not documented, so several shapes are accepted."""
    text = (text or "").strip()
    if not text:
        return None
    data = _json_or_none(text)
    if data is None:
        # Plain-text body: accept only something that looks like a token.
        return text if len(text) >= 20 and not any(ch.isspace() for ch in text) else None
    return _search_token(data)


def _search_token(obj):
    if isinstance(obj, dict):
        for key, value in obj.items():
            if str(key).lower() in TOKEN_KEYS and isinstance(value, str) and value.strip():
                return value.strip()
        for value in obj.values():
            found = _search_token(value)
            if found:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = _search_token(value)
            if found:
                return found
    return None


def redact(obj):
    """Mask secrets and large binary content so logs can be stored safely."""
    if isinstance(obj, dict):
        out = {}
        for key, value in obj.items():
            lowered = str(key).lower()
            if lowered in SECRET_KEYS:
                out[key] = "***"
            elif lowered in BINARY_KEYS and isinstance(value, str):
                out[key] = f"<{len(value)} chars of base64 omitted>"
            else:
                out[key] = redact(value)
        return out
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    return obj


def _redacted(data):
    return redact(data) if data is not None else None


def _log_response(text):
    if text is None:
        return None
    data = _json_or_none(text)
    if data is not None:
        return redact(data)
    return str(text)[:LOG_TEXT_LIMIT]


def _json_or_none(text):
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


def _decode_binary(value):
    if value is None or value == "" or value == []:
        return None
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    if isinstance(value, str):
        try:
            return base64.b64decode(value, validate=False)
        except (binascii.Error, ValueError):
            return None
    if isinstance(value, list):
        try:
            return bytes(value)
        except (TypeError, ValueError):
            return None
    return None