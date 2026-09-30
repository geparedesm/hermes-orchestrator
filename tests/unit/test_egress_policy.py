import pytest

from egress_proxy.policy import Policy, check_address, check_host, parse_domains

STANDARD = Policy("STANDARD")
ALLOW = Policy("ALLOWLIST", allowed_domains=("example.com", "*.pypi.org"), denied_domains=("prod.example.net",))


@pytest.mark.parametrize("host", ["example.com", "files.pypi.org", "EXAMPLE.com."])
def test_allowlisted_hosts(host):
    assert check_host(ALLOW, host, 443).allowed


@pytest.mark.parametrize(
    "host,port",
    [
        ("evil.example.org", 443),          # not allowlisted
        ("pypi.org", 443),                  # *.pypi.org does not match the apex
        ("example.com", 80),                # only 443
        ("example.com", 22),
        ("169.254.169.254", 443),           # IP literal
        ("[::1]", 443),
        ("localhost", 443),
        ("host.docker.internal", 443),
        ("db.internal", 443),
        ("printer.local", 443),
        ("not a host", 443),
    ],
)
def test_denied_requests(host, port):
    assert not check_host(ALLOW, host, port).allowed


def test_standard_mode_still_blocks_internal_names_and_denied_domains():
    assert check_host(STANDARD, "github.com", 443).allowed
    assert not check_host(STANDARD, "host.docker.internal", 443).allowed
    assert not check_host(Policy("STANDARD", denied_domains=("*.corp.example",)), "api.corp.example", 443).allowed


@pytest.mark.parametrize(
    "address",
    ["10.1.2.3", "172.17.0.1", "192.168.65.254", "127.0.0.1", "169.254.169.254", "100.64.0.1", "0.0.0.0",
     "224.0.0.1", "::1", "fe80::1", "fd00::1", "::ffff:10.0.0.1"],
)
def test_non_public_addresses_are_denied(address):
    assert not check_address(address).allowed


@pytest.mark.parametrize("address", ["93.184.215.14", "2606:2800:21f:cb07:6820:80da:af6b:8b2c"])
def test_public_addresses_are_allowed(address):
    assert check_address(address).allowed


def test_parse_domains():
    assert parse_domains(" Example.com, ,*.pypi.org,example.com") == ("*.pypi.org", "example.com")
    assert parse_domains(None) == ()


def test_unknown_mode_rejected():
    with pytest.raises(ValueError):
        Policy("OPEN")
