"""Asynchronous Python client for IPP."""
from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from importlib import metadata
from socket import gaierror
from struct import error as structerror
from typing import TYPE_CHECKING, Any

import aiohttp
from deepmerge import always_merger
from yarl import URL

from .const import (
    DEFAULT_CHARSET,
    DEFAULT_CHARSET_LANGUAGE,
    DEFAULT_PRINTER_ATTRIBUTES,
    DEFAULT_PROTO_VERSION,
)
from .enums import IppOperation, IppStatus
from .exceptions import (
    IPPConnectionError,
    IPPConnectionUpgradeRequired,
    IPPError,
    IPPParseError,
    IPPResponseError,
    IPPVersionNotSupportedError,
)
from .models import Printer
from .parser import parse as parse_response
from .serializer import encode_dict

if TYPE_CHECKING:
    from collections.abc import Mapping

if sys.version_info >= (3, 11):
    from asyncio import timeout
else:
    from async_timeout import timeout

VERSION = metadata.version(__package__)


@dataclass
class IPP:
    """Main class for handling connections with IPP servers."""

    host: str
    base_path: str = "/ipp/print"
    password: str | None = None
    port: int = 631
    request_timeout: int = 8
    session: aiohttp.client.ClientSession | None = None
    tls: bool = False
    username: str | None = None
    verify_ssl: bool = False
    user_agent: str | None = None
    ipp_version: tuple[int, int] = DEFAULT_PROTO_VERSION

    _close_session: bool = False
    _printer_uri: str = ""
    _printer: Printer | None = None

    def __post_init__(self) -> None:
        """Initialize connection parameters."""
        if self.host.startswith(("ipp://", "ipps://")):
            self._printer_uri = self.host
            printer_uri = URL(self.host)

            if printer_uri.host is not None:
                self.host = printer_uri.host

            if printer_uri.port is not None:
                self.port = printer_uri.port

            self.tls = printer_uri.scheme == "ipps"  # pylint: disable=W0143
            self.base_path = printer_uri.path
        else:
            self._printer_uri = self._build_printer_uri()

        if self.user_agent is None:
            self.user_agent = f"PythonIPP/{VERSION}"

    async def _request(
        self,
        uri: str = "",
        data: Any | None = None,
        params: Mapping[str, str] | None = None,
        doc: bytes | None = None,
    ) -> bytes:
        """Handle a request to an IPP server."""
        scheme = "https" if self.tls else "http"

        method = "POST"
        url = URL.build(
            scheme=scheme,
            host=self.host,
            port=self.port,
            path=self.base_path,
        ).join(URL(uri))

        auth = None
        if self.username and self.password:
            auth = aiohttp.BasicAuth(self.username, self.password)

        headers = {
            "User-Agent": self.user_agent,
            "Content-Type": "application/ipp",
            "Accept": "application/ipp, text/plain, */*",
        }

        if self.session is None:
            self.session = aiohttp.ClientSession()
            self._close_session = True

        if isinstance(data, dict):
            data = encode_dict(data, doc=doc)

        try:
            async with timeout(self.request_timeout):
                response = await self.session.request(
                    method,
                    url,
                    auth=auth,
                    data=data,
                    params=params,
                    headers=headers,
                    ssl=self.verify_ssl,
                )
        except asyncio.TimeoutError as exc:
            raise IPPConnectionError(
                f"Timeout occurred while connecting to IPP server {self.host}.",
            ) from exc
        except (aiohttp.ClientError, gaierror) as exc:
            raise IPPConnectionError(
                "Error occurred while communicating with IPP server.",
            ) from exc

        if response.status == 426:
            raise IPPConnectionUpgradeRequired(
                "Connection upgrade required while communicating with IPP server.",
                {"upgrade": response.headers.get("Upgrade")},
            )

        if (response.status // 100) in [4, 5]:
            content = await response.read()
            response.close()

            raise IPPResponseError(
                f"HTTP {response.status}",  # noqa: EM102
                {
                    "content-type": response.headers.get("Content-Type"),
                    "message": content.decode("utf8"),
                    "status-code": response.status,
                },
            )

        return await response.read()

    def _build_printer_uri(self) -> str:
        scheme = "ipps" if self.tls else "ipp"

        return URL.build(
            scheme=scheme,
            host=self.host,
            port=self.port,
            path=self.base_path,
        ).human_repr()

    def _message(self, operation: IppOperation, msg: dict[str, Any]) -> dict[str, Any]:
        """Build a request message to be sent to the server."""
        base = {
            "version": self.ipp_version,
            "operation": operation,
            "request-id": None,  # will get added by serializer if one isn't given
            "operation-attributes-tag": {  # these are required to be in this order
                "attributes-charset": DEFAULT_CHARSET,
                "attributes-natural-language": DEFAULT_CHARSET_LANGUAGE,
                "printer-uri": self._printer_uri,
                "requesting-user-name": "PythonIPP",
            },
        }

        return always_merger.merge(base, msg)

    async def execute(
        self,
        operation: IppOperation,
        message: dict[str, Any],
        doc: bytes | None = None,
    ) -> dict[str, Any]:
        """Send a request message to the server."""
        message = self._message(operation, message)
        response = await self._request(data=message, doc=doc)

        try:
            parsed = parse_response(response)
        except (structerror, Exception) as exc:  # disable=broad-except
            raise IPPParseError from exc

        if parsed["status-code"] == IppStatus.ERROR_VERSION_NOT_SUPPORTED:
            raise IPPVersionNotSupportedError("IPP version not supported by server")

        if parsed["status-code"] not in range(0x200):
            raise IPPError(
                "There has been an error",
                {
                    "status-code": parsed["status-code"],
                    "status": IppStatus(parsed["status-code"]).name,
                },
            )

        return parsed

    async def print_job(
        self,
        document: bytes,
        filename: str,
        sides: str,
        document_format: str = "application/octet-stream",
        copies: int = 1,
        fidelity: bool = False,
    ):
        """Print a document."""
        response_data = await self.execute(
            IppOperation.PRINT_JOB,
            {
                "operation-attributes-tag": {
                    "job-name": filename,
                    "document-format": document_format,
                    "ipp-attribute-fidelity": fidelity,
                },
                "job-attributes-tag": {
                    "copies": copies,
                    "sides": sides,
                },
            },
            doc=document,
        )

        return response_data

    async def validate_job(
        self,
        filename: str,
        sides: str,
        document_format: str = "application/octet-stream",
        copies: int = 1,
        fidelity: bool = False,
    ):
        """Validate that the printer can accept this job."""
        response_data = await self.execute(
            IppOperation.VALIDATE_JOB,
            {
                "operation-attributes-tag": {
                    "job-name": filename,
                    "document-format": document_format,
                    "ipp-attribute-fidelity": fidelity,
                },
                "job-attributes-tag": {
                    "copies": copies,
                    "sides": sides,
                },
            },
        )

        return response_data

    async def get_jobs(
        self,
        which_jobs: str = "not-completed",
        job_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """Get printer jobs."""
        response_data = await self.execute(
            IppOperation.GET_JOBS,
            {
                "operation-attributes-tag": {
                    "which-jobs": which_jobs,
                    "requested-attributes": "all",
                },
            },
        )

        return response_data["jobs"]

    async def get_all_jobs(
        self,
    ) -> list[dict[str, Any]]:
        """Get all printer jobs."""
        not_completed_res = self.get_jobs()
        completed_res = self.get_jobs(which_jobs="completed")

        async with asyncio.TaskGroup() as tg:
            not_completed_data = tg.create_task(not_completed_res)
            completed_res = tg.create_task(completed_res)

        return not_completed_data.result() + completed_res.result()

    async def get_job_attributes(
        self,
        job_id: int,
    ) -> list[dict[str, Any]]:
        """Get job attributes by job ID."""
        response_data = await self.execute(
            IppOperation.GET_JOB_ATTRIBUTES,
            {
                "operation-attributes-tag": {
                    "job-id": job_id,
                    "requested-attributes": "all",
                },
            },
        )

        return response_data["jobs"]

    async def cancel_job(
        self,
        job_id: int,
        job_uri: str,
        printer_uri: str,
    ) -> list[dict[str, Any]]:
        """Cancell a print job."""
        response_data = await self.execute(
            IppOperation.CANCEL_JOB,
            {
                "operation-attributes-tag": {
                    "job-id": job_id,
                    "printer-uri": printer_uri,
                },
            },
        )

        return response_data["jobs"]

    async def get_printer_attributes(
        self,
    ) -> dict[str, Any]:
        """Get printer attributes."""
        return await self.execute(
            IppOperation.GET_PRINTER_ATTRIBUTES,
            {
                "operation-attributes-tag": {
                    "requested-attributes": "all",
                },
            },
        )

    async def raw(self, operation: IppOperation, message: dict[str, Any]) -> bytes:
        """Send a request message to the server and return raw response."""
        message = self._message(operation, message)

        return await self._request(data=message)

    async def close(self) -> None:
        """Close open client session."""
        if self.session and self._close_session:
            await self.session.close()

    async def printer(self) -> Printer:
        """Get printer information from server."""
        response_data = await self.execute(
            IppOperation.GET_PRINTER_ATTRIBUTES,
            {
                "operation-attributes-tag": {
                    "requested-attributes": DEFAULT_PRINTER_ATTRIBUTES,
                },
            },
        )

        parsed: dict[str, Any] = next(iter(response_data["printers"] or []), {})

        try:
            if self._printer is None:
                self._printer = Printer.from_dict(parsed)
            else:
                self._printer.update_from_dict(parsed)
        except Exception as exc:  # noqa: BLE001
            print("Error parsing printer response", exc)
            raise IPPParseError from exc

        return self._printer

    async def __aenter__(self) -> IPP:  # noqa: PYI034
        """Async enter."""
        return self

    async def __aexit__(self, *_exec_info: object) -> None:
        """Async exit."""
        await self.close()
