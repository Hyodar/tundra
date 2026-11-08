"""The Ed25519 check of the checksum database's tree head: RFC 8032 section 5.1.7, strict."""

from __future__ import annotations

import hashlib

import pytest

from tundravm import _go_toolchain as go

RFC_1 = (
    "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a",
    "",
    "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701c"
    "f9b46bd25bf5f0595bbe24655141438e7a100b",
)
RFC_2 = (
    "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
    "72",
    "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0"
    "f11d8c387b2eaeb4302aeeb00d291612bb0c00",
)
RFC_3 = (
    "fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025",
    "af82",
    "6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac18ff9b538d16f290ae67f76098"
    "4dc6594a7c15e9716ed28dc027beceea1ec40a",
)
RFC_1024 = (
    "278117fc144c72340f67d0f2316e8386ceffbf2b2428c9c51fef7c597f1d426e",
    (
        "08b8b2b733424243760fe426a4b54908632110a66c2f6591eabd3345e3e4eb98fa6e264bf09efe12ee50f8f5"
        "4e9f77b1e355f6c50544e23fb1433ddf73be84d879de7c0046dc4996d9e773f4bc9efe5738829adb26c81b37"
        "c93a1b270b20329d658675fc6ea534e0810a4432826bf58c941efb65d57a338bbd2e26640f89ffbc1a858efc"
        "b8550ee3a5e1998bd177e93a7363c344fe6b199ee5d02e82d522c4feba15452f80288a821a579116ec6dad2b"
        "3b310da903401aa62100ab5d1a36553e06203b33890cc9b832f79ef80560ccb9a39ce767967ed628c6ad573c"
        "b116dbefefd75499da96bd68a8a97b928a8bbc103b6621fcde2beca1231d206be6cd9ec7aff6f6c94fcd7204"
        "ed3455c68c83f4a41da4af2b74ef5c53f1d8ac70bdcb7ed185ce81bd84359d44254d95629e9855a94a7c1958"
        "d1f8ada5d0532ed8a5aa3fb2d17ba70eb6248e594e1a2297acbbb39d502f1a8c6eb6f1ce22b3de1a1f40cc24"
        "554119a831a9aad6079cad88425de6bde1a9187ebb6092cf67bf2b13fd65f27088d78b7e883c8759d2c4f5c6"
        "5adb7553878ad575f9fad878e80a0c9ba63bcbcc2732e69485bbc9c90bfbd62481d9089beccf80cfe2df16a2"
        "cf65bd92dd597b0707e0917af48bbb75fed413d238f5555a7a569d80c3414a8d0859dc65a46128bab27af87a"
        "71314f318c782b23ebfe808b82b0ce26401d2e22f04d83d1255dc51addd3b75a2b1ae0784504df543af8969b"
        "e3ea7082ff7fc9888c144da2af58429ec96031dbcad3dad9af0dcbaaaf268cb8fcffead94f3c7ca495e056a9"
        "b47acdb751fb73e666c6c655ade8297297d07ad1ba5e43f1bca32301651339e22904cc8c42f58c30c04aafdb"
        "038dda0847dd988dcda6f3bfd15c4b4c4525004aa06eeff8ca61783aacec57fb3d1f92b0fe2fd1a85f672451"
        "7b65e614ad6808d6f6ee34dff7310fdc82aebfd904b01e1dc54b2927094b2db68d6f903b68401adebf5a7e08"
        "d78ff4ef5d63653a65040cf9bfd4aca7984a74d37145986780fc0b16ac451649de6188a7dbdf191f64b5fc5e"
        "2ab47b57f7f7276cd419c17a3ca8e1b939ae49e488acba6b965610b5480109c8b17b80e1b7b750dfc7598d5d"
        "5011fd2dcc5600a32ef5b52a1ecc820e308aa342721aac0943bf6686b64b2579376504ccc493d97e6aed3fb0"
        "f9cd71a43dd497f01f17c0e2cb3797aa2a2f256656168e6c496afc5fb93246f6b1116398a346f1a641f3b041"
        "e989f7914f90cc2c7fff357876e506b50d334ba77c225bc307ba537152f3f1610e4eafe595f6d9d90d11faa9"
        "33a15ef1369546868a7f3a45a96768d40fd9d03412c091c6315cf4fde7cb68606937380db2eaaa707b4c4185"
        "c32eddcdd306705e4dc1ffc872eeee475a64dfac86aba41c0618983f8741c5ef68d3a101e8a3b8cac60c905c"
        "15fc910840b94c00a0b9d0"
    ),
    "0aab4c900501b3e24d7cdf4663326a3a87df5e4843b2cbdb67cbf6e460fec350aa5371b1508f9f4528ecea23c4"
    "36d94b5e8fcd4f681e30a6ac00a9704a188a03",
)
SECRET_1 = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
"""RFC 8032 test 1's secret key, which signs the rows that need a signer's cooperation."""
ZERO = bytes(32)
IDENTITY = (1).to_bytes(32, "little")

Signed = tuple[bytes, bytes, bytes]


def _vector(row: tuple[str, str, str]) -> Signed:
    return bytes.fromhex(row[0]), bytes.fromhex(row[1]), bytes.fromhex(row[2])


def _flip(data: bytes, bit: int) -> bytes:
    out = bytearray(data)
    out[bit // 8] ^= 1 << (bit % 8)
    return bytes(out)


def _changed(row: tuple[str, str, str], part: int, bit: int) -> Signed:
    """*row* with *bit* of its key (0), message (1) or signature (2) flipped."""
    signed = list(_vector(row))
    signed[part] = _flip(signed[part], bit)
    return signed[0], signed[1], signed[2]


def _malleable(row: tuple[str, str, str]) -> Signed:
    """*row* with ``S + L`` for ``S``: the same point equation, the same ``[S]B``."""
    public, message, signature = _vector(row)
    s = int.from_bytes(signature[32:], "little") + go._L
    return public, message, signature[:32] + s.to_bytes(32, "little")


def _with_r(r: bytes, message: bytes = b"tree") -> Signed:
    """A signature by :data:`SECRET_1` whose ``R`` is the encoding *r* of the identity.

    ``S = k * a`` makes ``[S]B = R + [k]A`` hold, so only the checks on ``R`` refuse it.
    """
    h = hashlib.sha512(SECRET_1).digest()
    a = (int.from_bytes(h[:32], "little") & ((1 << 254) - 8)) | (1 << 254)
    public = go.point_compress(go.point_mul(a, go.BASE))
    k = int.from_bytes(hashlib.sha512(r + public + message).digest(), "little") % go._L
    return public, message, r + (k * a % go._L).to_bytes(32, "little")


def _zero_key_forgery() -> Signed:
    """``R`` the identity and ``S = 0`` under the all-zero key, a point of order 4: the
    equation holds for every message whose ``k`` is a multiple of 4."""
    for n in range(64):
        message = b"go.sum database tree\n%d\n" % n
        k = int.from_bytes(hashlib.sha512(IDENTITY + ZERO + message).digest(), "little")
        if k % go._L % 4 == 0:
            return ZERO, message, IDENTITY + ZERO
    raise AssertionError("no message in 64 has k = 0 mod 4")


@pytest.mark.parametrize(
    ("signed", "valid"),
    [
        (_vector(RFC_1), True),
        (_vector(RFC_2), True),
        (_vector(RFC_3), True),
        (_vector(RFC_1024), True),
        (_changed(RFC_2, 2, 3), False),
        (_changed(RFC_2, 2, 300), False),
        (_changed(RFC_2, 1, 0), False),
        (_changed(RFC_1024, 1, 8183), False),
        (_changed(RFC_3, 0, 9), False),
        (_malleable(RFC_3), False),
        (_malleable(RFC_1024), False),
        (_with_r(IDENTITY), False),
        (_with_r((1 + go._P).to_bytes(32, "little")), False),
        (_with_r((1 | 1 << 255).to_bytes(32, "little")), False),
        (_zero_key_forgery(), False),
        ((ZERO, *_vector(RFC_1)[1:]), False),
        ((_vector(RFC_1)[0][:31], *_vector(RFC_1)[1:]), False),
        ((*_vector(RFC_1)[:2], _vector(RFC_1)[2][:63]), False),
    ],
    ids=[
        "rfc-1",
        "rfc-2",
        "rfc-3",
        "rfc-1024",
        "r-bit-flipped",
        "s-bit-flipped",
        "message-bit-flipped",
        "message-1024-last-bit-flipped",
        "key-bit-flipped",
        "s-plus-l",
        "s-plus-l-1024",
        "r-small-order",
        "r-non-canonical-y",
        "r-negative-zero-x",
        "zero-key-forgery",
        "zero-key",
        "short-key",
        "short-signature",
    ],
)
def test_only_a_signature_the_key_made_verifies(signed: Signed, valid: bool) -> None:
    """RFC 8032's vectors verify; a changed bit, ``S >= L``, an ``R`` or key of small
    order, and a non-canonical ``R`` do not."""
    public, message, signature = signed
    assert go.ed25519_verify(public, message, signature) is valid


def _aliases() -> list[tuple[bytes, bytes]]:
    """``(canonical, alias)`` for each point with two encodings: ``y + p`` for ``y < 19``,
    and ``x = 0`` with the sign bit, which RFC 8032 5.1.3 step 4 refuses."""
    pairs = [
        (y.to_bytes(32, "little"), (y + go._P).to_bytes(32, "little"))
        for y in range(19)
        if go._decompress(y.to_bytes(32, "little")) is not None
    ]
    for y in (1, go._P - 1):
        pairs.append((y.to_bytes(32, "little"), (y | 1 << 255).to_bytes(32, "little")))
    return pairs


@pytest.mark.parametrize(("canonical", "alias"), _aliases())
def test_a_point_decodes_from_its_canonical_encoding_only(canonical: bytes, alias: bytes) -> None:
    point = go._decompress(canonical)
    assert point is not None and go.point_compress(point) == canonical
    assert go._decompress(alias) is None


def test_the_base_point_is_rfc_8032s_of_order_l() -> None:
    assert go.point_compress(go.BASE) == bytes.fromhex("58" + "66" * 31)
    order_l = go.point_mul(go._L, go.BASE)
    assert order_l[0] % go._P == 0 and (order_l[1] - order_l[2]) % go._P == 0
