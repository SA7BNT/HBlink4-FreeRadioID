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

Alternatively, point https.cert_file / https.key_file at certificates you
manage yourself and set https.letsencrypt to false.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
from pathlib import Path
from typing import Any, Dict, Optional

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, PlainTextResponse, RedirectResponse
from starlette.routing import Route

logger = logging.getLogger(__name__)

_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{10,200}$")
_DOMAIN_RE = re.compile(r"^(?=.{4,253}$)([A-Za-z0-9-]{1,63}\.)+[A-Za-z]{2,63}$")


class CertManager:
    def __init__(self, cfg: Dict[str, Any], data_dir: Path):
        self.cfg = cfg or {}
        self.domain: str = str(self.cfg.get("domain") or "").strip().lower()
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

    def https_url(self, path: str = "/") -> str:
        port = "" if self.https_port == 443 else f":{self.https_port}"
        return f"https://{self.domain}{port}{path}"

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
            args = self._certbot_base() + [
                "certonly", "--webroot", "-w", str(self.webroot),
                "-d", self.domain, "--cert-name", self.domain, "--agree-tos", "--keep-until-expiring"]
        except RuntimeError as e:
            logger.error(f"🔒 {e}")
            return False
        args += ["--email", self.email] if self.email else ["--register-unsafely-without-email"]
        if self.staging:
            args.append("--staging")
        logger.info(f"🔒 Requesting Let's Encrypt certificate for {self.domain} …")
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
        return RedirectResponse(cm.https_url(request.url.path + query), status_code=301)

    return Starlette(routes=[
        Route("/.well-known/acme-challenge/{token}", challenge),
        Route("/{path:path}", other, methods=["GET", "HEAD", "POST"]),
    ])
