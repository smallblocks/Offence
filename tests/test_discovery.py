import time

import httpx
import pytest

from offence.app import create_app
from offence.crypto import Identity
from offence.discovery import Discovery, peer_url
from offence.models import Advertisement
from offence.store import Store


def ad(key, seq=1, now=None):
    now = int(time.time()) if now is None else now
    return key.sign(Advertisement(issued=now, expires=now + 180, sequence=seq,
                                 endpoint="https://8.8.8.8:8080").model_dump())


def test_discovery_expiry_replay_and_capacity(tmp_path):
    store = Store(tmp_path / "db", max_peers=1)
    one, two = Identity(), Identity()
    assert store.ingest(ad(one))
    assert not store.ingest(ad(one))
    assert store.ingest(ad(two))
    assert store.peers()[0]["signer"] == two.public
    assert store.ingest(ad(one, seq=2))
    with pytest.raises(ValueError):
        store.ingest(ad(one, now=int(time.time()) - 400))
    assert store.peers(now=int(time.time()) + 200) == []
    store.close()


@pytest.mark.parametrize("url", ["http://127.0.0.1", "https://169.254.169.254", "https://10.0.0.1",
    "https://[::1]", "https://example.com", "https://user:password@8.8.8.8", "https://8.8.8.8/admin"])
def test_gossip_cannot_trigger_lan_probes(url):
    with pytest.raises(ValueError):
        peer_url(url)


def test_exact_operator_approved_lan_peer():
    assert peer_url("http://127.0.0.1:8080", ["http://127.0.0.1:8080"]) == "http://127.0.0.1:8080"
    assert peer_url("http://" + "a" * 56 + ".onion")
    with pytest.raises(ValueError):
        peer_url("http://127.0.0.1:8081", ["http://127.0.0.1:8080"])


async def test_three_nodes_discover_transitively_without_directory(config, tmp_path):
    apps = []
    for i in range(3):
        cfg = config.model_copy(deep=True)
        cfg.endpoint = f"http://127.0.0.1:{8100+i}"
        cfg.allowed_private_peers = [f"http://127.0.0.1:{8100+j}" for j in range(3)]
        apps.append(create_app(tmp_path / str(i), cfg, background=False))
    a, b, c = [app.state.discovery for app in apps]
    for discovery in (a, b, c):
        discovery.advertisement()
    a.client = lambda endpoint: httpx.AsyncClient(transport=httpx.ASGITransport(app=apps[1]), base_url=endpoint)
    await a.exchange(b.config.endpoint)
    c.client = lambda endpoint: httpx.AsyncClient(transport=httpx.ASGITransport(app=apps[0]), base_url=endpoint)
    await c.exchange(a.config.endpoint)
    assert {p["signer"] for p in c.store.peers()} == {a.identity.public, b.identity.public, c.identity.public}
    # Seed-free restart retains discovered peers and identity.
    identity = c.identity.public
    c.store.close()
    restarted = create_app(tmp_path / "2", c.config, background=False)
    assert restarted.state.discovery.identity.public == identity
    assert len(restarted.state.store.peers()) == 3
