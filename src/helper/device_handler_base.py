"""Shared base class for all Hive device handlers."""

import asyncio
import logging
from typing import Any

from .const import HIVE_TYPES, HIVETOHA, HTTP_OK, LIGHT_GROUP_NODE_TYPE
from .hive_exceptions import HiveAuthError, HiveRateLimited
from .hivedataclasses import Device

_LOGGER = logging.getLogger(__name__)


class BaseDeviceHandler:  # pylint: disable=too-few-public-methods
    """Common plumbing shared across all Hive device handler classes.

    Subclasses must ensure ``self.session`` is set before any method is called.
    """

    session: Any

    async def _send_state_with_recovery(
        self, product_type: str, device: Device, state_kwargs
    ) -> dict | None:
        """Send the state change, renewing tokens and retrying once on 401/403.

        Returns the API response, or None after a timeout, a 429 or a second
        rejection. Returning None rather than raising means one failed device
        doesn't abort a multi-entity service call in Home Assistant.
        """
        token_seen = self.session.tokens.token_created
        set_state = self.session.api.set_state
        try:
            try:
                return await set_state(product_type, device.hive_id, **state_kwargs)
            except HiveAuthError:
                _LOGGER.warning(
                    "_execute_state_change - Hive rejected the token for %s, "
                    "renewing it and retrying once",
                    device.ha_name,
                )
                await self.session.recover_auth(token_seen)
                return await set_state(product_type, device.hive_id, **state_kwargs)
        except asyncio.TimeoutError:
            _LOGGER.warning(
                "_execute_state_change - set_state timed out for %s", device.ha_name
            )
        except HiveRateLimited:
            _LOGGER.warning(
                "_execute_state_change - Hive rate limited the change for %s",
                device.ha_name,
            )
        except HiveAuthError:
            _LOGGER.error(
                "_execute_state_change - Hive still rejected the change for %s "
                "after renewing the token",
                device.ha_name,
            )
        return None

    async def _execute_state_change(self, device: Device, **state_kwargs) -> bool:
        """Check online → refresh tokens → set_state → refresh device data.

        Returns True on HTTP 200, False on failure or when device is
        unavailable.  Uses the product type stored in session.data so callers
        never need to read it themselves.

        A 401/403 renews the tokens and retries once. A 429 or a second
        rejection returns False. Only ``HiveReauthRequired`` is raised.
        """
        if device.hive_id not in self.session.data.products:
            _LOGGER.debug(
                "_execute_state_change - %s not found in products", device.ha_name
            )
            return False
        if not (
            isinstance(device.device_data, dict) and device.device_data.get("online")
        ):
            _LOGGER.debug(
                "_execute_state_change - %s is offline or device_data not initialised",
                device.ha_name,
            )
            return False
        await self.session.hive_refresh_tokens()
        product = self.session.data.products[device.hive_id]
        product_type = product["type"]
        if product.get("isGroup") and product_type in HIVE_TYPES["Light"]:
            # Hive's app changes a light group at /nodes/productgroup/<id>;
            # posting to /nodes/<light type>/<id> gets 400 MALFORMED_REQUEST.
            product_type = LIGHT_GROUP_NODE_TYPE
        resp = await self._send_state_with_recovery(product_type, device, state_kwargs)
        if resp is None:
            return False
        if resp.get("original") == HTTP_OK:
            await self.session.refresh_after_change()
            return True
        _LOGGER.error(
            "_execute_state_change - set_state failed for %s: HTTP %s",
            device.ha_name,
            resp.get("original"),
        )
        return False

    def _get_product_state(self, device: Device, *path_keys, default=None):
        """Read a nested value from session.data.products[device.hive_id].

        Returns *default* (None by default) if any key in *path_keys* is
        missing rather than raising KeyError.
        """
        try:
            node = self.session.data.products[device.hive_id]
            for key in path_keys:
                node = node[key]
            return node
        except (KeyError, TypeError):
            return default

    def _map_hive_to_ha(self, mapping_key: str, value, fallback=None):
        """Translate a raw Hive API value through HIVETOHA[mapping_key].

        Returns *fallback* when the key is absent from the mapping, or the
        original *value* when *fallback* is None.
        """
        mapping = HIVETOHA.get(mapping_key, {})
        if value in mapping:
            return mapping[value]
        return fallback if fallback is not None else value
