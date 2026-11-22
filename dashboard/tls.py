"""
HTTPS for the dashboard with automatic Let's Encrypt certificates.

How it works
------------
* A small HTTP app listens on https.http_port (80): it answers Let's Encrypt
  HTTP-01 challenges from a webroot and redirects everything else to HTTPS.
* certbot (``sudo apt install certbot``) obtains the certificate in webroot
  mode. It runs as the dashboard's own user with its config/work/log dirs in
  dashboard/data/letsencrypt, so no root is needed.
* Twice a day ``certbot renew`` runs; a renewed certificate is loaded into the
  running server without a restart.
* Ports below 1024 need CAP_NET_BIND_SERVICE (set in hblink4-dash.service).

Additional names (the APRS map domains, e.g. aprs.die-n8schicht.de) go into
the same certificate as extra names (SAN). Before they are requested, each is
looked up in DNS: a name that does not point to the same address as the main
domain is left out (with a log line) instead of making the whole request fail.

Alternatively, point https.cert_file / https.key_file at certificates you
manage yourself and set https.letsencrypt to false.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import socket
import ssl
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, PlainTextResponse, RedirectResponse
from starlette.routing import Route

logger = logging.getLogger(__name__)

_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{10,200}$")
_DOMAIN_RE = re.compile(r"^(?=.{4,253}$)([A-Za-z0-9-]{1,63}\.)+[A-Za-z]{2,63}$")


class CertManager:
    def __init__(self, cfg: Dict[str, Any], data_dir: Path, extra_domains: Iterable[str] = ()):
        self.cfg = cfg or {}
        self.domain: str = str(self.cfg.get("domain") or "").strip().lower()
        self.extra: List[str] = []
        for d in extra_domains or ():
            d = str(d or "").strip().lower().rstrip(".")
            if d and d != self.domain and _DOMAIN_RE.match(d) and d not in self.extra:
                self.extra.append(d)
        self.requested: List[str] = []    # extra names that went into the last request
        self.email: str = str(self.cfg.get("email") or "").strip()
        self.letsencrypt: bool = bool(self.cfg.get("letsencrypt", True))
        self.staging: bool = bool(self.cfg.get("staging", False))
        self.https_port: int = int(self.cfg.get("port", 443))
        self.base = Path(data_dir) / "letsencrypt"
        self.webroot = self.base / "webroot"
        self.webroot.mkdir(parents=True, exist_ok=True)
        if self.letsencrypt:
            live = self.base / "config" / "live" / self.domain
            self.cert_file = str(live / "fullchain.pem")
            self.key_file = str(live / "privkey.pem")
        else:
            self.cert_file = str(self.cfg.get("cert_file") or "")
            self.key_file = str(self.cfg.get("key_file") or "")
        self._mtime = self._cert_mtime()

    # ---------------------------------------------------------------- state
    def valid_domain(self) -> bool:
        return bool(_DOMAIN_RE.match(self.domain))

    def have_cert(self) -> bool:
        return bool(self.cert_file and self.key_file and os.path.isfile(self.cert_file)
                    and os.path.isfile(self.key_file))

    def _cert_mtime(self) -> float:
        try:
            return os.stat(self.cert_file).st_mtime  # follows certbot's symlink
        except OSError:
            return 0.0

    @property
    def all_domains(self) -> List[str]:
        return [self.domain] + self.extra

    def https_url(self, path: str = "/", host: str = "") -> str:
        port = "" if self.https_port == 443 else f":{self.https_port}"
        host = (host or "").split(":")[0].lower()
        name = host if host in self.all_domains else self.domain
        return f"https://{name}{port}{path}"

    def cert_names(self) -> Set[str]:
        """DNS names in the current certificate (empty if unknown)."""
        if not self.have_cert():
            return set()
        try:
            info = ssl._ssl._test_decode_cert(self.cert_file)  # type: ignore[attr-defined]
            return {v.lower() for k, v in info.get("subjectAltName", ()) if k == "DNS"}
        except Exception:
            return set()

    def missing_names(self) -> List[str]:
        """Extra names that the certificate does not cover yet."""
        if not self.letsencrypt or not self.extra:
            return []
        names = self.cert_names()
        return [d for d in self.extra if d not in names] if names else list(self.extra)

    @staticmethod
    def _addresses(name: str) -> Set[str]:
        try:
            return {ai[4][0] for ai in socket.getaddrinfo(name, None, proto=socket.IPPROTO_TCP)}
        except OSError:
            return set()

    async def resolvable_extra(self) -> List[str]:
        """Extra names that point to the same address(es) as the main domain."""
        if not self.extra:
            return []
        main = await asyncio.to_thread(self._addresses, self.domain)
        ok = []
        for d in self.extra:
            addrs = await asyncio.to_thread(self._addresses, d)
            if addrs and (not main or addrs & main):
                ok.append(d)
            else:
                logger.warning(f"🔒 {d} is left out of the certificate: it does not point to this server "
                               f"({', '.join(sorted(addrs)) or 'no DNS entry'}; {self.domain}: "
                               f"{', '.join(sorted(main)) or '?'})")
        return ok

    # -------------------------------------------------------------- certbot
    def _certbot_base(self) -> list:
        exe = shutil.which("certbot")
        if not exe:
            raise RuntimeError("certbot is not installed (sudo apt install certbot)")
        return [exe,
                "--config-dir", str(self.base / "config"),
                "--work-dir", str(self.base / "work"),
                "--logs-dir", str(self.base / "logs"),
                "--non-interactive"]

    async def _run(self, args: list) -> int:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=300)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            # never leave certbot running behind a stopping dashboard
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            logger.error("🔒 certbot did not finish within 5 minutes (or the dashboard is stopping) - aborted")
            raise
        for line in out.decode(errors="replace").splitlines()[-25:]:
            if line.strip():
                logger.info(f"certbot: {line}")
        return proc.returncode

    async def obtain(self) -> bool:
        """Request a certificate. The HTTP challenge listener must already be up."""
        if not self.letsencrypt:
            return self.have_cert()
        if not self.valid_domain():
            logger.error(f"🔒 https.domain {self.domain!r} is not a valid public domain name")
            return False
        try:
            extra = await self.resolvable_extra()
            names = [self.domain] + extra
            args = self._certbot_base() + [
                "certonly", "--webroot", "-w", str(self.webroot),
                "--cert-name", self.domain, "--agree-tos", "--keep-until-expiring", "--expand"]
            for n in names:
                args += ["-d", n]
            self.requested = extra
        except RuntimeError as e:
            logger.error(f"🔒 {e}")
            return False
        args += ["--email", self.email] if self.email else ["--register-unsafely-without-email"]
        if self.staging:
            args.append("--staging")
        logger.info(f"🔒 Requesting Let's Encrypt certificate for {', '.join(names)} …")
        try:
            rc = await self._run(args)
        except asyncio.TimeoutError:
            return False
        if rc == 0 and self.have_cert():
            self._mtime = self._cert_mtime()
            logger.info(f"🔒 Certificate for {self.domain} is ready")
            return True
        logger.error(f"🔒 certbot failed (exit {rc}). Is port 80 reachable from the internet and does "
                     f"{self.domain} point to this server?")
        return False

    async def renew(self) -> bool:
        """Run certbot renew; returns True if the certificate file changed."""
        if not self.letsencrypt:
            changed = self._cert_mtime() != self._mtime
            self._mtime = self._cert_mtime()
            return changed
        try:
            await self._run(self._certbot_base() + ["renew", "--cert-name", self.domain])
        except asyncio.TimeoutError:
            return False
        except RuntimeError as e:
            logger.error(f"🔒 {e}")
            return False
        mtime = self._cert_mtime()
        changed = mtime != self._mtime
        self._mtime = mtime
        if changed:
            logger.info(f"🔒 Certificate for {self.domain} renewed")
        return changed


def build_http_app(cm: CertManager, redirect: bool = True) -> Starlette:
    """ACME challenge responder + redirect to HTTPS."""

    async def challenge(request: Request):
        token = request.path_params["token"]
        path = cm.webroot / ".well-known" / "acme-challenge" / token
        if not _TOKEN_RE.match(token) or not path.is_file():
            return PlainTextResponse("not found", status_code=404)
        return FileResponse(str(path), media_type="text/plain")

    async def other(request: Request):
        if not redirect:
            return PlainTextResponse("HTTPS only", status_code=404)
        query = f"?{request.url.query}" if request.url.query else ""
        return RedirectResponse(cm.https_url(request.url.path + query, request.headers.get("host", "")),
                                status_code=301)

    return Starlette(routes=[
        Route("/.well-known/acme-challenge/{token}", challenge),
        Route("/{path:path}", other, methods=["GET", "HEAD", "POST"]),
    ])
