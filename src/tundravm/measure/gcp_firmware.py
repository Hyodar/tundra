"""GCP TDX firmware table: the values MRTD and RTMR0 take on Google's hosts.

Ported from ``flashbots/dstack-mr-gcp`` at ``b16e08b`` (``internal/consts.go``,
``internal/mr.go``), the tool flashbots-images pins for ``make measure-gcp``. Google
publishes each firmware at ``gs://gce_tcb_integrity/ovmf_x64_csm/<sha384>.fd`` and its
MRTD under ``…/tdx/``; ``CFV`` is the SHA-384 of each firmware's configuration firmware
volume, computed once from the published ``.fd`` (the file name is its own SHA-384). TD
HOB and ACPI hashes are per machine type, captured from live quotes by dstack-mr-gcp's
maintainers. A new Google firmware or machine type needs a new tundravm release.
"""

from __future__ import annotations

from dataclasses import dataclass

TABLE_VERSION = "dstack-mr-gcp@b16e08b"
FIRMWARE_URL = "https://storage.googleapis.com/gce_tcb_integrity/ovmf_x64_csm/{sha384}.fd"
MRTD_URL = "https://storage.googleapis.com/gce_tcb_integrity/ovmf_x64_csm/tdx/{sha384}.binarypb"


@dataclass(frozen=True, slots=True)
class Firmware:
    sha384: str
    mrtd: str
    cfv: str

    @property
    def label(self) -> str:
        return f"gcp-ovmf-{self.sha384[:12]}"


FIRMWARES = (
    Firmware(
        "ff11d313b462e2c7b08143f54785f77f71f55f71673d934c8e209c086294548c8c61eff4b3a9dd0708e3e5d79e163f39",
        "a5844e88897b70c318bef929ef4dfd6c7304c52c4bc9c3f39132f0fdccecf3eb5bab70110ee42a12509a31c037288694",
        "48061a7b81efc2143937223cc61cdbbd960d4e6b2a4260c62d54b1a37e15c1e12ef41225d221d2b885e3b2cee8979e65",
    ),
    Firmware(
        "afa88de3952c1b3f696fb5c526493742ff25131848bd769305c69166103a53790d010ffb34149b5f428df6eaa39a2749",
        "8370d8f6d02f2d13e211e91c93fde923049522b241425a29a7bf0071ef49b250af4ef49d852fa3e10065d1b51dfce8fb",
        "16a03d3d47d197945e85080880f6af2d87355f3d1eae2e27295d286e2ce7da3df4128d5d20a31d4c2cb3b20e91aecbca",
    ),
    Firmware(
        "f53fdf89544e1e6d785eee42d0a4bb38e26b36e951be537ac22114d210f2d5239eba243dd71991afe8345e7020974a46",
        "feb7486608382c1ff0e15b4648ddc0acea6ca974eb53e3529f4c4bd5ffbaa20bf335cb75965cea65fe473aed9647c162",
        "9cb6bf09aea7b4acb8549e328d0edd6f15defc0b00d744bb9fb5bab0962bc5c70f69d233e96dbc7c1105ba085781dc88",
    ),
)

SECURE_BOOT_VARIABLES = (
    "cfa4e2c606f572627bf06d5669cc2ab1128358d27b45bc63ee9ea56ec109cfafb7194006f847a6a74b5eaed6b73332ec",
    "905f6243baf0d7c63cd672f89b16e15f99597e8d0392955e685172d447100123f7c490d178543922faddf896625dabab",
    "be013b0d9188e72b870f598899c35864d6b25f029a7b5f21a037bacf61ca3646207af2bc714d471407c9939317763c4a",
    "723ad4d64f430bf6d325ab9d6c29147993ded5630002e42e13df696ebc680c4bc14c392d2e113e141154e21723f890f6",
    "c61bae1a3f7b7e6cc3b9b03f630b77292ebd232ae60e0e1916f980955ec38459529574b49f1898c367eaf6d8a62311f5",
)
"""SecureBoot (off), PK, KEK, db and dbx: the EFI variable events in RTMR0, in order."""
BOOT_ORDER = bytes((0x01, 0x00, 0x02, 0x00, 0x00, 0x00))
BOOT_0001 = "a25333c7aec2e0993034938c7f11893b3c2bcaf67e88c342a3d586f6f7fae2c6a1247a9ed86988080a6d4be497d4fbb6"  # noqa: E501
BOOT_0002 = "9068065754ff3ae3dd58a5897535eeaf62a19a6757d82dd91349c41bae2e3f208e268abba2a4378bc5c8d1acf2fd260f"  # noqa: E501
BOOT_0000 = "23ada07f5261f12f34a0bd8e46760962d6b4d576a416f1fea1c64bc656b1d28eacf7047ae6e967c58fd2a98bfa74c298"  # noqa: E501

_ACPI_C3_4 = {
    "pre-2026-03": (
        "f29e022d067da7289c935eec2477c46032f2e0659a11e658f0ae05068aea420189e77e302b715084dacc7c0c4c118ea6",
        "509dcfe10beb5d470c40f25e30895370948831b9cf79db15d977e7bba8eb42f7200212071ad8b19d6011759779eced5a",
        "0f0b426cbd5bd9c2a4a5e640e6c4556b8df071dcc973dcf95465ae5514c0eba6dc960a6b7b21d29099cada75dc1b46d5",
    ),
    "2026-03": (
        "02d9b85e7df1e92d15d85988b161fdf60626bff433af8055f367b134260d59f6a0dab0965254627aecb1367373d74b3a",
        "ba3706e632a5cb239f1c85febafcfeadebdfe3ed1a4d716d46387ad0803b4c4a7fecbcad2c05cd390e5f4f3548e303dc",
        "d221bae226ed9811ec5977c8f15dbe8c97ff80d67bbd4246d5ecf60f7c61081e597e18df601a657a46cbdeb773ab8f88",
    ),
    "2026-04": (
        "595afd922c90ebbe335c7973d0bd3ffcc7820b41f81929ad80b3b5e919b51fc07c0ff23ebb14088563e2b2886412415c",
        "f877abf8389a35d12290902a3d769a970bbbc16dc6fc568817c09fd6534c2752f3bcff8c2afeb65f8be93e248e58052d",
        "7605e3e2cad7be3aff8d19dba4fbaf8b12b3322cf9d1afdd1587ec75c12588c628595008b49243d58843ed0e1bad63e9",
    ),
}
_ACPI_C3_8 = {
    "2026-04": (
        "817ed5d198c9f02b43cab233916b71c0a02b7c27507dc2331a619bf314635a4d82f52a69dc29ce2490694b1058147cd8",
        "98ee59933be736e33f9d2d5be7777c9f30fdf93056a4893638f559c130bfe852d63d5559b2e72f5e1b7e48f35657b8c4",
        "7d11e4c6983fbf869814962a69bac39a6cd5947467841de602a169af3f9a77974ee740f62336a94bf694f056476fa32d",
    ),
}
_ACPI_C3_22 = {
    "2026-03": (
        "29342ab42c2814109d694110d1c9a7558e08c7c87e478b348fc4944fcae2105c708813dc0a01fd29a2189d760d41ad03",
        "440eef6064096f13201108d30c9b466c0c6b6ed207c8028f7425442c164e80080d8a9bbb6f6b56be48fdf2c830d99f86",
        "2a42acbb09b7d0e59b59e09f775cb5b7748d0906d87bb886cf87081b6aa1213e42bb745e1ac19064c0cea19a6cfa5aeb",
    ),
    "2026-04": (
        "e604eb632926426ea42bf22c97845e499b116d9988d163897c06595c501d924ba4b32600df960a70482c5e7665fde31f",
        "fa3427613d7ea513391e308dd91f254d52744131e080d01e5c09c170e897edef34afb1f83efea74d6729bdad929032bf",
        "b9d101b755cf100437205fa4cdec9660e451d31d3707eba607d5e2aea8cd76976f71a9ede56d091787e7a81189b3a549",
    ),
}
_ACPI_C3_44 = {
    "pre-2026-03": (
        "8b8256b4dd618ea330a3c499fd211b9fa681b8205c470013b9c13ca3263c5da5b2e253f89ee0ca5111a41f8054856df3",
        "c104e3179249648403077f70784b26c9844394177179145e28844b2dc7e40b34d0fad0ba22b3a3cef00197d2bd58f4c4",
        "c4ae73d7e84f15aa17f7fcf50c83b2846e7e6628cff7f993531b6d8de129bd84359e234feeaf5e4c4a4c87a167161960",
    ),
    "2026-03": (
        "0fb22ea8315f50939d88e835e425b083b4e87c5a738b6b09e03191d0583bba7e35091de84770ce643bd17828c96a50e6",
        "eb2ecd4e4ff962f12888ed1e0209437d1c1dbd9cce050286e10dd79b33d000075bd4287b6066c482a1d4851c07485e97",
        "0381c7a53b1fa332467e6ea7240fb38b8ea5e6740af0b5c496c8d66e0647da7706b6e149b9fd00c420572b419ffc9608",
    ),
    "2026-04": (
        "7a40e09ee7fe41bac83bf518a611f2d223bd433189e0757cc9893646a850b77578266ff53652f38f2b28b91e3471e708",
        "f1466ad49807c50b0df671c155c03a85abce04aad460c8a078f412299056834b3dc3bcbb8b0816d86997eedc21bce1e0",
        "71f14359dc15c2ba4f9c31fcc04f75d57849d3c0476ab72aef4dbff02182ff8eb7c18dd8d89cc6def235dc2c56a6ee49",
    ),
}

MACHINE_TYPES: dict[str, tuple[str, dict[str, tuple[str, str, str]]]] = {
    "c3-standard-4": (
        "458994daa60deac8dea19dba79748f6ff93fd0aebb8e3e0be5a65eb12309d342c3ce31cc67af7bbd22af1a44e7d9fe21",
        _ACPI_C3_4,
    ),
    "c3-standard-8": (
        "aa9e81feeb58a9eb3a9f4110cc7b5696240437ea4c1a9c30518cfc44fa305183e6473e6bc02ddc4de09d0c49c49fadb5",
        _ACPI_C3_8,
    ),
    "c3-standard-22": (
        "a5be8ecd74020972e328fbbe94d2886817ef0d2e8a4e94e9572e8e1b221f3f608cddc868cf8b08e8e645e4aaeba68279",
        _ACPI_C3_22,
    ),
    "c3-standard-44": (
        "21092eadb73948aebb405b826354c23c3025635c89a8d91f85905afb120b7d98025a6c3083e8e82b5320695b253ce341",
        _ACPI_C3_44,
    ),
}
"""Machine type: (TD HOB hash, {ACPI table set: (loader, RSDP, tables)})."""
