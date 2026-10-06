"""Push notifications to the owner's phones and browsers (Web Push): prints that finish, pause or fail.

Each browser that turns notifications on registers a subscription, kept in <data>/push.json. The
server signs its messages with its own key (VAPID), made on first start in <data>/vapid.pem.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import secrets
from collections import OrderedDict
from pathlib import Path

import aiohttp
from cryptography.hazmat.primitives import serialization
from py_vapid import Vapid02
from pywebpush import WebPushException, webpush_async

log = logging.getLogger(__name__)

PHOTOS_KEPT = 20  # notification photos served from memory, newest last
TTL = 6 * 3600  # seconds a push service keeps trying to deliver: after that the news is stale


class Push:
    def __init__(self, data_dir: Path, contact: str):
        self.path = data_dir / "push.json"
        self.contact = contact  # push services want a way to reach whoever sends the messages
        key_file = data_dir / "vapid.pem"
        if not key_file.exists():
            vapid = Vapid02()
            vapid.generate_keys()
            key_file.write_bytes(vapid.private_pem())
            key_file.chmod(0o600)
        self.vapid = Vapid02.from_pem(key_file.read_bytes())
        point = self.vapid.public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        self.public_key = base64.urlsafe_b64encode(point).rstrip(b"=").decode()
        try:
            self.subscriptions: list[dict] = json.loads(self.path.read_text())
        except (OSError, ValueError):
            self.subscriptions = []
        self.photos: OrderedDict[str, bytes] = OrderedDict()

    def add(self, subscription: dict, label: str) -> None:
        self.subscriptions = [s for s in self.subscriptions if s["endpoint"] != subscription["endpoint"]]
        self.subscriptions.append({**subscription, "label": label})
        self._save()

    def remove(self, endpoint: str) -> None:
        self.subscriptions = [s for s in self.subscriptions if s["endpoint"] != endpoint]
        self._save()

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.subscriptions, indent=2))
        tmp.chmod(0o600)
        tmp.replace(self.path)

    def photo(self, token: str) -> bytes | None:
        return self.photos.get(token)

    def _keep_photo(self, image: bytes) -> str:
        """The phone's notification system fetches the image without a login, so it gets a random link."""
        token = secrets.token_urlsafe(16)
        self.photos[token] = image
        while len(self.photos) > PHOTOS_KEPT:
            self.photos.popitem(last=False)
        return token

    async def notify(self, title: str, body: str = "", *, tag: str | None = None, photo: bytes | None = None,
                     only: str | None = None) -> None:
        """Send to every subscribed device (or only the one with endpoint `only`)."""
        targets = [s for s in self.subscriptions if only is None or s["endpoint"] == only]
        if not targets:
            return
        message = {"title": title, "body": body, "tag": tag, "url": "/"}
        if photo:
            message["image"] = f"/api/push/photos/{self._keep_photo(photo)}.jpg"
        data = json.dumps(message)
        results = await asyncio.gather(*(self._send(s, data) for s in targets))
        gone = {s["endpoint"] for s, delivered in zip(targets, results) if delivered is False}
        if gone:
            log.info("push: removing %d expired subscription(s)", len(gone))
            self.subscriptions = [s for s in self.subscriptions if s["endpoint"] not in gone]
            self._save()

    async def _send(self, subscription: dict, data: str) -> bool | None:
        """True when delivered, False when the subscription no longer exists, None on other errors."""
        try:
            await webpush_async(
                {"endpoint": subscription["endpoint"], "keys": subscription["keys"]},
                data,
                vapid_private_key=self.vapid,
                vapid_claims={"sub": self.contact},  # a new dict each time: webpush adds aud/exp to it
                ttl=TTL,
                timeout=10,
                headers={"Urgency": "high"},
            )
            return True
        except WebPushException as e:
            if e.response is not None and e.response.status in (404, 410):
                return False
            log.warning("push to %s failed: %s", subscription.get("label") or "a device", str(e).splitlines()[0])
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError) as e:
            log.warning("push to %s failed: %s", subscription.get("label") or "a device", e)
        return None
