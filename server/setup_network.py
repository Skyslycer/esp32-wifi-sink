"""Public setup defaults without assuming a proxy address is the audio server."""
import ipaddress


def usable_ipv4(value):
    try:
        address = ipaddress.IPv4Address(value)
    except (ipaddress.AddressValueError, ValueError, TypeError):
        return False
    return not (address.is_loopback or address.is_link_local or address.is_multicast or
                address.is_reserved or address.is_unspecified or address.packed[0] == 0)


def advertised_address(value):
    if value is None or not value.strip():
        return None
    value = value.strip()
    if not usable_ipv4(value):
        raise ValueError('SINK_ADVERTISED_IPV4 must be a usable IPv4 address without a scheme or port')
    return str(ipaddress.IPv4Address(value))

