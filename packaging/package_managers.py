#!/usr/bin/env python3
"""Render a Homebrew formula and winget manifests for one release.

    python packaging/package_managers.py v0.2.0 checksums.txt -o out/

`checksums.txt` is the file the release workflow publishes next to the
binaries. This writes:

    out/Formula/mclean.rb                         -> copy into a Homebrew tap repo
    out/winget/RishikeshSreekumar.mclean.*.yaml   -> PR to microsoft/winget-pkgs

Publishing is a manual step: a tap is its own repository
(RishikeshSreekumar/homebrew-tap -> `brew install rishikeshsreekumar/tap/mclean`),
and winget manifests go through a pull request to microsoft/winget-pkgs.
"""

from __future__ import annotations

import argparse
from pathlib import Path

REPO = "RishikeshSreekumar/gmail_cleaner"
DESC = "Local mailbox observability and cleanup dashboard (metadata only, no delete)"
HOMEPAGE = f"https://github.com/{REPO}"


def read_checksums(path: Path) -> dict[str, str]:
    out = {}
    for line in path.read_text().splitlines():
        parts = line.split()
        if len(parts) == 2:
            out[parts[1].lstrip("*")] = parts[0]
    return out


def formula(tag: str, sums: dict[str, str]) -> str:
    version = tag.lstrip("v")
    url = f"https://github.com/{REPO}/releases/download/{tag}"

    def asset(os_name: str, arch: str) -> tuple[str, str]:
        name = f"mclean-{tag}-{os_name}-{arch}.tar.gz"
        if name not in sums:
            raise SystemExit(f"{name} is not in the checksums file")
        return f"{url}/{name}", sums[name]

    mac_arm, mac_intel = asset("macos", "arm64"), asset("macos", "x86_64")
    lin_arm, lin_intel = asset("linux", "arm64"), asset("linux", "x86_64")
    return f'''class Mclean < Formula
  desc "{DESC}"
  homepage "{HOMEPAGE}"
  version "{version}"
  license "MIT"

  on_macos do
    on_arm do
      url "{mac_arm[0]}"
      sha256 "{mac_arm[1]}"
    end
    on_intel do
      url "{mac_intel[0]}"
      sha256 "{mac_intel[1]}"
    end
  end

  on_linux do
    on_arm do
      url "{lin_arm[0]}"
      sha256 "{lin_arm[1]}"
    end
    on_intel do
      url "{lin_intel[0]}"
      sha256 "{lin_intel[1]}"
    end
  end

  def install
    bin.install "mclean"
  end

  test do
    assert_match "Gmail", shell_output("#{{bin}}/mclean providers")
  end
end
'''


def winget(tag: str, sums: dict[str, str]) -> dict[str, str]:
    version = tag.lstrip("v")
    name = f"mclean-{tag}-windows-x86_64.zip"
    if name not in sums:
        raise SystemExit(f"{name} is not in the checksums file")
    pid = "RishikeshSreekumar.mclean"
    head = f"PackageIdentifier: {pid}\nPackageVersion: {version}\n"
    return {
        f"{pid}.yaml": head + "DefaultLocale: en-US\nManifestType: version\nManifestVersion: 1.6.0\n",
        f"{pid}.installer.yaml": head + f"""InstallerType: zip
NestedInstallerType: portable
NestedInstallerFiles:
  - RelativeFilePath: mclean.exe
    PortableCommandAlias: mclean
Installers:
  - Architecture: x64
    InstallerUrl: https://github.com/{REPO}/releases/download/{tag}/{name}
    InstallerSha256: {sums[name].upper()}
ManifestType: installer
ManifestVersion: 1.6.0
""",
        f"{pid}.locale.en-US.yaml": head + f"""PackageLocale: en-US
Publisher: Rishikesh Sreekumar
PackageName: mclean
License: MIT
ShortDescription: {DESC}
PackageUrl: {HOMEPAGE}
ManifestType: defaultLocale
ManifestVersion: 1.6.0
""",
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("tag")
    ap.add_argument("checksums", type=Path)
    ap.add_argument("-o", "--out", type=Path, default=Path("package-managers"))
    a = ap.parse_args()
    sums = read_checksums(a.checksums)
    (a.out / "Formula").mkdir(parents=True, exist_ok=True)
    (a.out / "Formula" / "mclean.rb").write_text(formula(a.tag, sums))
    (a.out / "winget").mkdir(parents=True, exist_ok=True)
    for fname, text in winget(a.tag, sums).items():
        (a.out / "winget" / fname).write_text(text)
    print(f"wrote {a.out}/Formula/mclean.rb and {a.out}/winget/")


if __name__ == "__main__":
    main()
