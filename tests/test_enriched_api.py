"""Tests for enriched API surface matching SPEC and DESIGN docs."""

from pathlib import Path

import pytest

from tdx import Image, Kernel, SecretTarget, ValidationError

# --- Rich service() parameters ---


class TestServiceSpec:
    def test_service_with_exec_and_user(self) -> None:
        img = Image()
        img.service(
            "nethermind",
            exec=["/opt/nethermind/nethermind", "--config", "/etc/nm/config.json"],
            user="nethermind",
            after=["network-online.target"],
            requires=["network-online.target"],
            restart="always",
        )
        svc = img.state.profiles["default"].services[0]
        assert svc.name == "nethermind"
        assert svc.exec == ("/opt/nethermind/nethermind", "--config", "/etc/nm/config.json")
        assert svc.user == "nethermind"
        assert svc.after == ("network-online.target",)
        assert svc.requires == ("network-online.target",)
        assert svc.restart == "always"

    def test_service_exec_string_split(self) -> None:
        img = Image()
        img.service("app", exec="/usr/bin/app --flag value")
        svc = img.state.profiles["default"].services[0]
        assert svc.exec == ("/usr/bin/app", "--flag", "value")

    def test_service_extra_unit(self) -> None:
        img = Image()
        img.service(
            "heavy",
            exec=["/usr/bin/heavy"],
            extra_unit={"Service": {"MemoryMax": "8G", "LimitNOFILE": "65535"}},
        )
        svc = img.state.profiles["default"].services[0]
        assert svc.extra_unit["Service"]["MemoryMax"] == "8G"

    def test_service_security_profile_strict(self) -> None:
        img = Image()
        img.service("secure", exec=["/usr/bin/secure"], security_profile="strict")
        svc = img.state.profiles["default"].services[0]
        assert svc.security_profile == "strict"

    def test_duplicate_service_name_rejected(self) -> None:
        img = Image()
        img.service("app")
        with pytest.raises(ValidationError, match="Duplicate service"):
            img.service("app")

    def test_same_service_name_in_different_profiles_ok(self) -> None:
        img = Image()
        img.service("app")
        with img.profile("dev"):
            img.service("app")  # Different profile, should be fine


# --- Rich user() parameters ---


class TestUserSpec:
    def test_user_system_home_groups(self) -> None:
        img = Image()
        img.user(
            "nethermind",
            system=True,
            home="/var/lib/nethermind",
            uid=800,
            groups=["eth", "tdx"],
        )
        user = img.state.profiles["default"].users[0]
        assert user.name == "nethermind"
        assert user.system is True
        assert user.home == "/var/lib/nethermind"
        assert user.uid == 800
        assert user.groups == ("eth", "tdx")

    def test_duplicate_user_name_rejected(self) -> None:
        img = Image()
        img.user("app", system=True)
        with pytest.raises(ValidationError, match="Duplicate user"):
            img.user("app", system=True)

    def test_same_user_name_in_different_profiles_ok(self) -> None:
        img = Image()
        img.user("app")
        with img.profile("dev"):
            img.user("app")  # Different profile, should be fine


# --- Template with src= file loading ---


class TestTemplateSrc:
    def test_template_from_file(self, tmp_path: Path) -> None:
        tmpl = tmp_path / "config.j2"
        tmpl.write_text("network={network}\nport={port}\n", encoding="utf-8")

        img = Image()
        img.template(
            "/etc/app/config.toml",
            src=tmpl,
            vars={"network": "mainnet", "port": 8545},
        )
        entry = img.state.profiles["default"].templates[0]
        assert entry.path == "/etc/app/config.toml"
        assert entry.rendered == "network=mainnet\nport=8545\n"

    def test_template_inline_with_vars(self) -> None:
        img = Image()
        img.template(
            "/etc/motd",
            template="Welcome to {name}\n",
            vars={"name": "TDX VM"},
        )
        entry = img.state.profiles["default"].templates[0]
        assert entry.rendered == "Welcome to TDX VM\n"

    def test_template_legacy_variables_param(self) -> None:
        img = Image()
        img.template(
            "/etc/app/env",
            template="A={a}\n",
            variables={"a": "1"},
        )
        entry = img.state.profiles["default"].templates[0]
        assert entry.rendered == "A=1\n"

    def test_template_src_and_template_mutually_exclusive(self, tmp_path: Path) -> None:
        tmpl = tmp_path / "t.j2"
        tmpl.write_text("x", encoding="utf-8")
        img = Image()
        with pytest.raises(ValidationError, match="exactly one"):
            img.template("/etc/x", src=tmpl, template="y")

    def test_template_requires_src_or_template(self) -> None:
        img = Image()
        with pytest.raises(ValidationError, match="src= or template="):
            img.template("/etc/x")


# --- Rich repository() parameters ---


class TestRepositorySpec:
    def test_repository_with_suite_components_keyring(self) -> None:
        img = Image()
        img.repository(
            "https://packages.microsoft.com/debian/12/prod",
            suite="bookworm",
            components=["main"],
            keyring="./keys/microsoft.gpg",
        )
        repo = img.state.profiles["default"].repositories[0]
        assert repo.url == "https://packages.microsoft.com/debian/12/prod"
        assert repo.suite == "bookworm"
        assert repo.components == ("main",)
        assert repo.keyring == "./keys/microsoft.gpg"

    def test_repository_auto_name_from_url(self) -> None:
        img = Image()
        img.repository("https://example.com/my-repo")
        repo = img.state.profiles["default"].repositories[0]
        assert repo.name == "my-repo"

    def test_repository_explicit_name(self) -> None:
        img = Image()
        img.repository("https://example.com/repo", name="custom-name")
        repo = img.state.profiles["default"].repositories[0]
        assert repo.name == "custom-name"


# --- Rich debloat() parameters ---


class TestDebloatConfig:
    def test_debloat_default_paths(self) -> None:
        img = Image()
        img.debloat()
        config = img.state.profiles["default"].debloat
        assert config.enabled is True
        assert "/usr/share/doc" in config.paths_remove
        assert "/usr/share/man" in config.paths_remove
        assert config.systemd_minimize is True

    def test_debloat_paths_skip(self) -> None:
        img = Image()
        img.debloat(paths_skip=["/usr/share/bash-completion"])
        config = img.state.profiles["default"].debloat
        assert "/usr/share/bash-completion" not in config.effective_paths_remove

    def test_debloat_paths_remove_extra(self) -> None:
        img = Image()
        img.debloat(paths_remove_extra=["/usr/share/fonts"])
        config = img.state.profiles["default"].debloat
        assert "/usr/share/fonts" in config.effective_paths_remove

    def test_debloat_systemd_minimize_false(self) -> None:
        img = Image()
        img.debloat(systemd_minimize=False)
        config = img.state.profiles["default"].debloat
        assert config.systemd_minimize is False

    def test_debloat_systemd_units_keep_extra(self) -> None:
        img = Image()
        img.debloat(systemd_units_keep_extra=["systemd-resolved.service"])
        config = img.state.profiles["default"].debloat
        assert "systemd-resolved.service" in config.effective_units_keep

    def test_debloat_disabled(self) -> None:
        img = Image()
        img.debloat(enabled=False)
        config = img.state.profiles["default"].debloat
        assert config.enabled is False

    def test_explain_debloat_includes_rich_fields(self) -> None:
        img = Image()
        img.debloat(
            paths_skip=["/usr/share/bash-completion"],
            systemd_units_keep_extra=["systemd-resolved.service"],
        )
        explanation = img.explain_debloat()
        assert "systemd_minimize" in explanation
        assert "systemd_units_keep" in explanation
        assert "systemd_bins_keep" in explanation
        assert "paths_skip" in explanation


# --- Lifecycle convenience methods ---


class TestLifecycleMethods:
    def test_sync(self) -> None:
        img = Image()
        img.sync("git submodule update --init")
        assert "sync" in img.state.profiles["default"].phases
        assert img.state.profiles["default"].phases["sync"][0].argv == (
            "git submodule update --init",
        )

    def test_prepare(self) -> None:
        img = Image()
        img.prepare("pip install pyyaml")
        assert "prepare" in img.state.profiles["default"].phases

    def test_finalize(self) -> None:
        img = Image()
        img.finalize("du -sh $BUILDROOT")
        assert "finalize" in img.state.profiles["default"].phases

    def test_postoutput(self) -> None:
        img = Image()
        img.postoutput("sha256sum $OUTPUTDIR/latest.efi")
        assert "postoutput" in img.state.profiles["default"].phases

    def test_clean(self) -> None:
        img = Image()
        img.clean("rm -rf ./tmp-cache")
        assert "clean" in img.state.profiles["default"].phases

    def test_on_boot(self) -> None:
        img = Image()
        img.on_boot("/usr/local/bin/init-attestation")
        assert "boot" in img.state.profiles["default"].phases

    def test_skeleton_as_file(self, tmp_path: Path) -> None:
        img = Image()
        img.skeleton("/etc/resolv.conf", content="nameserver 1.1.1.1\n")
        assert img.state.profiles["default"].skeleton_files[0].path == "/etc/resolv.conf"

    def test_ssh_installs_dropbear(self) -> None:
        img = Image()
        img.ssh(enabled=True)
        assert "dropbear" in img.state.profiles["default"].packages

    def test_sync_requires_command(self) -> None:
        img = Image()
        with pytest.raises(TypeError):
            img.sync()  # type: ignore[call-arg]

    def test_prepare_requires_command(self) -> None:
        img = Image()
        with pytest.raises(TypeError):
            img.prepare()  # type: ignore[call-arg]


# --- Kernel model ---


class TestKernel:
    def test_kernel_generic(self) -> None:
        k = Kernel.generic("6.8")
        assert k.version == "6.8"
        assert k.tdx is False

    def test_kernel_tdx(self) -> None:
        k = Kernel.tdx_kernel("6.8")
        assert k.version == "6.8"
        assert k.tdx is True

    def test_kernel_from_config(self) -> None:
        k = Kernel.from_config("./my.config")
        assert k.config_file == "./my.config"

    def test_image_kernel_assignment(self) -> None:
        img = Image()
        img.kernel = Kernel.tdx_kernel("6.8", cmdline="console=ttyS0")
        assert img.kernel is not None
        assert img.kernel.version == "6.8"
        assert img.kernel.tdx is True
        assert img.kernel.cmdline == "console=ttyS0"

    def test_kernel_tdx_with_config_file(self) -> None:
        k = Kernel.tdx_kernel("6.13.12", config_file="kernel/kernel-yocto.config")
        assert k.version == "6.13.12"
        assert k.tdx is True
        assert k.config_file == "kernel/kernel-yocto.config"
        assert k.source_repo == "https://github.com/gregkh/linux"

    def test_kernel_tdx_with_custom_source_repo(self) -> None:
        k = Kernel.tdx_kernel(
            "6.13.12",
            config_file="kernel.config",
            source_repo="https://github.com/custom/linux",
        )
        assert k.source_repo == "https://github.com/custom/linux"
        assert k.config_file == "kernel.config"

    def test_kernel_default_source_repo(self) -> None:
        k = Kernel.tdx_kernel("6.8")
        assert k.source_repo == "https://github.com/gregkh/linux"


# --- Public exports ---


class TestPublicExports:
    def test_secret_schema_importable(self) -> None:
        from tdx import SecretSchema

        schema = SecretSchema(kind="string", min_length=8)
        assert schema.min_length == 8

    def test_secret_target_importable(self) -> None:
        from tdx import SecretTarget

        t = SecretTarget.file("/run/secrets/key")
        assert t.kind == "file"

    def test_kernel_importable(self) -> None:
        from tdx import Kernel

        k = Kernel(version="6.8")
        assert k.version == "6.8"

    def test_debloat_config_importable(self) -> None:
        from tdx import DebloatConfig

        c = DebloatConfig()
        assert c.enabled is True


# --- Image constructor enrichment ---


class TestImageConstructor:
    def test_image_target_and_backend(self) -> None:
        img = Image(target="aarch64", backend="local_linux", reproducible=False)
        assert img.target == "aarch64"
        assert img.backend == "local_linux"
        assert img.reproducible is False


# --- Secret owner support ---


class TestSecretTargetOwner:
    def test_secret_file_with_owner(self) -> None:
        t = SecretTarget.file("/run/secrets/jwt.hex", owner="nethermind", mode="0440")
        assert t.owner == "nethermind"
        assert t.mode == "0440"

    def test_secret_file_without_owner(self) -> None:
        t = SecretTarget.file("/run/secrets/key")
        assert t.owner is None
