# -*- coding: utf-8 -*-
"""HTTP transport. The client depends only on the interface, so tests can replace it."""

from dataclasses import dataclass


class TransportError(Exception):
    """Connection failed or the response could not be read."""


class TransportTimeout(TransportError):
    """No response within the timeout."""


@dataclass
class HttpResponse:
    status: int
    text: str


class RequestsTransport:
    def request(self, method, url, *, headers, json_body, timeout):
        import requests  # imported here so the pure library stays importable without it

        try:
            resp = requests.request(method, url, headers=headers, json=json_body, timeout=timeout)
        except requests.Timeout as exc:
            raise TransportTimeout(str(exc)) from exc
        except requests.RequestException as exc:
            raise TransportError(str(exc)) from exc
        return HttpResponse(status=resp.status_code, text=resp.text)