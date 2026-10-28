"""Install a Thunderbird autoconfig that imports ign8mail credentials.

Thunderbird supports an "autoconfig" .cfg file placed in the install directory
that runs with full chrome privileges on every startup. We use it to call
`Services.logins.addLogin()` directly — this is the same path Thunderbird's own
UI uses, so the stored credential is guaranteed to be matched when TB tries to
authenticate against IMAP/SMTP.

Flow:
  1. One-time install: drop `autoconfig_ign8mail.js` into TB's `defaults/pref/`
     and `ign8mail.cfg` into TB's Resources dir (needs sudo).
  2. On every `ign8mail reset-passwords`, drop a credentials JSON into the
     profile directory. The autoconfig picks it up on next TB startup, adds
     all logins via the LoginManager API, then deletes the credentials file.
"""

import json
import platform
import subprocess
import tempfile
from pathlib import Path


class AutoconfigInstallError(RuntimeError):
    pass


_PREF_FILENAME = "autoconfig_ign8mail.js"
_CFG_FILENAME = "ign8mail.cfg"

_PREF_CONTENTS = """\
// ign8mail autoconfig loader — managed by `ign8mail reset-passwords`
pref("general.config.filename", "ign8mail.cfg");
pref("general.config.obscure_value", 0);
pref("general.config.sandbox_enabled", false);
"""

# First line of a Thunderbird autoconfig .cfg is skipped (historical convention),
# so we burn it on an explanatory banner. IOUtils/PathUtils aren't present in
# the autoconfig context — we use nsIFile + nsIFileInputStream for everything.
_CFG_CONTENTS = r"""// ign8mail autoconfig — do not edit by hand
(function () {
  function readJSONFile(path) {
    const file = Cc["@mozilla.org/file/local;1"].createInstance(Ci.nsIFile);
    file.initWithPath(path);
    if (!file.exists()) return null;
    const stream = Cc["@mozilla.org/network/file-input-stream;1"]
      .createInstance(Ci.nsIFileInputStream);
    stream.init(file, 0x01, 0o444, 0);
    const cstream = Cc["@mozilla.org/intl/converter-input-stream;1"]
      .createInstance(Ci.nsIConverterInputStream);
    cstream.init(stream, "UTF-8", 0, 0);
    let text = "";
    const buf = {};
    let n;
    while ((n = cstream.readString(0xffffffff, buf)) !== 0) text += buf.value;
    cstream.close();
    return JSON.parse(text);
  }

  function removeFile(path) {
    const file = Cc["@mozilla.org/file/local;1"].createInstance(Ci.nsIFile);
    file.initWithPath(path);
    if (file.exists()) file.remove(false);
  }

  const observer = {
    observe(subject, topic) {
      Services.obs.removeObserver(this, "final-ui-startup");
      (async () => {
        try {
          const profileDir = Services.dirsvc.get("ProfD", Ci.nsIFile).path;
          const credsPath = profileDir + "/ign8mail_credentials.json";
          const data = readJSONFile(credsPath);
          if (!data) return;

          const nsLoginInfo = Components.Constructor(
            "@mozilla.org/login-manager/loginInfo;1",
            Ci.nsILoginInfo,
            "init"
          );
          let added = 0;
          let removed = 0;
          for (const c of data.credentials || []) {
            for (const scheme of ["imap", "smtp"]) {
              const host = scheme === "imap" ? c.imap_host : c.smtp_host;
              const origin = scheme + "://" + c.username + "@" + host;
              // TB's own IMAP/SMTP entries use httpRealm == origin — if we
              // pass null, TB won't match the record during auth lookup.
              const existing = await Services.logins.searchLoginsAsync({ origin });
              for (const l of existing) {
                try { await Services.logins.removeLoginAsync(l); removed++; } catch (e) {}
              }
              const info = new nsLoginInfo(origin, null, origin, c.username, c.password, "", "");
              try {
                await Services.logins.addLoginAsync(info);
                added++;
              } catch (e) {
                Cu.reportError("ign8mail addLogin failed for " + origin + ": " + e);
              }
            }
          }
          removeFile(credsPath);
          Services.console.logStringMessage(
            "ign8mail autoconfig: added " + added + " logins (removed " + removed + " stale)."
          );
        } catch (e) {
          try { Cu.reportError("ign8mail autoconfig run: " + e + "\n" + (e.stack || "")); } catch (ignore) {}
        }
      })();
    },
  };
  try {
    Services.obs.addObserver(observer, "final-ui-startup");
  } catch (e) {
    try { Cu.reportError("ign8mail autoconfig setup: " + e); } catch (ignore) {}
  }
})();
"""


def resources_dir() -> Path | None:
    """Return the Thunderbird Resources directory, or None if not found."""
    if platform.system() == "Darwin":
        d = Path("/Applications/Thunderbird.app/Contents/Resources")
        return d if d.is_dir() else None
    if platform.system() == "Linux":
        for d in (
            Path("/usr/lib/thunderbird"),
            Path("/usr/lib64/thunderbird"),
            Path("/opt/thunderbird"),
        ):
            if d.is_dir():
                return d
    return None


def is_installed(resources: Path | None = None) -> bool:
    resources = resources or resources_dir()
    if resources is None:
        return False
    pref = resources / "defaults" / "pref" / _PREF_FILENAME
    cfg = resources / _CFG_FILENAME
    if not pref.exists() or not cfg.exists():
        return False
    try:
        return cfg.read_text() == _CFG_CONTENTS and pref.read_text() == _PREF_CONTENTS
    except OSError:
        return False


def install(resources: Path | None = None, use_sudo: bool = True) -> None:
    """Install (or refresh) the autoconfig files. Uses `sudo` by default."""
    resources = resources or resources_dir()
    if resources is None:
        raise AutoconfigInstallError("Thunderbird install not found.")
    pref_path = resources / "defaults" / "pref" / _PREF_FILENAME
    cfg_path = resources / _CFG_FILENAME

    pref_tmp = tempfile.NamedTemporaryFile("w", delete=False, suffix=".js")
    pref_tmp.write(_PREF_CONTENTS)
    pref_tmp.close()
    cfg_tmp = tempfile.NamedTemporaryFile("w", delete=False, suffix=".cfg")
    cfg_tmp.write(_CFG_CONTENTS)
    cfg_tmp.close()

    def run(cmd: list[str]) -> None:
        full = (["sudo"] if use_sudo else []) + cmd
        res = subprocess.run(full, capture_output=True, text=True)
        if res.returncode != 0:
            raise AutoconfigInstallError(
                f"{' '.join(full)} failed: {res.stderr.strip() or res.stdout.strip()}"
            )

    run(["mkdir", "-p", str(pref_path.parent)])
    run(["install", "-m", "644", pref_tmp.name, str(pref_path)])
    run(["install", "-m", "644", cfg_tmp.name, str(cfg_path)])

    Path(pref_tmp.name).unlink(missing_ok=True)
    Path(cfg_tmp.name).unlink(missing_ok=True)


def uninstall(resources: Path | None = None, use_sudo: bool = True) -> None:
    resources = resources or resources_dir()
    if resources is None:
        return
    pref_path = resources / "defaults" / "pref" / _PREF_FILENAME
    cfg_path = resources / _CFG_FILENAME
    for p in (pref_path, cfg_path):
        if p.exists():
            cmd = (["sudo"] if use_sudo else []) + ["rm", "-f", str(p)]
            subprocess.run(cmd, check=False)


def write_credentials(
    profile_dir: Path,
    imap_host: str,
    smtp_host: str,
    credentials: dict[str, str],
) -> Path:
    """Drop the credentials file the autoconfig consumes on next startup.

    File is written atomically with 0600 perms. The autoconfig deletes it after
    processing.
    """
    data = {
        "credentials": [
            {"username": u, "password": p, "imap_host": imap_host, "smtp_host": smtp_host}
            for u, p in sorted(credentials.items())
        ],
    }
    target = profile_dir / "ign8mail_credentials.json"
    tmp = profile_dir / ".ign8mail_credentials.json.tmp"
    tmp.write_text(json.dumps(data))
    tmp.chmod(0o600)
    tmp.replace(target)
    return target
