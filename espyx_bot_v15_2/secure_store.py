"""
EspyX Bot — guardado seguro de contraseñas.

La contraseña NUNCA se escribe en texto plano en la carpeta del bot. Se usa, en este orden:
  1. La librería `keyring` si está instalada (Administrador de credenciales de Windows / Llavero de macOS / Secret Service).
  2. Windows sin keyring: cifrado DPAPI del propio Windows (solo tu usuario de Windows, en este PC, puede descifrarla).
  3. macOS sin keyring: el Llavero (comando `security`, viene con el sistema).
  4. Otros sistemas: archivo en tu carpeta de usuario con permisos solo para ti, ofuscado (no es cifrado fuerte;
     la ventana lo avisa).
"""
import base64
import hashlib
import json
import os
import subprocess
import sys

SERVICE = "EspyX Bot"


def user_dir():
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        d = os.path.join(base, "EspyX")
    elif sys.platform == "darwin":
        d = os.path.expanduser("~/Library/Application Support/EspyX")
    else:
        d = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "espyx")
    os.makedirs(d, exist_ok=True)
    return d


# ------------------------------------------------------------------ backends
class _Keyring:
    name = "el almacén de contraseñas del sistema (keyring)"
    secure = True

    def __init__(self):
        import keyring  # noqa: F401 (falla si no está instalada)
        from keyring.backends import fail
        kr = keyring.get_keyring()
        if isinstance(kr, fail.Keyring) or "null" in type(kr).__name__.lower():
            raise RuntimeError("sin backend de keyring")
        self.k = keyring
        if sys.platform.startswith("win"):
            self.name = "el Administrador de credenciales de Windows"
        elif sys.platform == "darwin":
            self.name = "el Llavero de macOS"

    def save(self, email, pw):
        self.k.set_password(SERVICE, email, pw)

    def load(self, email):
        return self.k.get_password(SERVICE, email)

    def delete(self, email):
        try:
            self.k.delete_password(SERVICE, email)
        except Exception:
            pass


class _FileBackend:
    """Guarda {correo: secreto} en un archivo de la carpeta del usuario. Subclases definen cómo se protege el secreto."""
    secure = True

    def __init__(self):
        self.path = os.path.join(user_dir(), "credenciales.json")

    def _read(self):
        try:
            with open(self.path, encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            return {}

    def _write(self, data):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        try:
            os.chmod(tmp, 0o600)
        except Exception:
            pass
        os.replace(tmp, self.path)

    def save(self, email, pw):
        data = self._read()
        data[email.lower()] = self._protect(pw)
        self._write(data)

    def load(self, email):
        blob = self._read().get(email.lower())
        if not blob:
            return None
        try:
            return self._unprotect(blob)
        except Exception:
            return None

    def delete(self, email):
        data = self._read()
        if data.pop(email.lower(), None) is not None:
            self._write(data)


class _DPAPI(_FileBackend):
    name = "cifrada con la protección de datos de Windows (DPAPI)"

    def __init__(self):
        if not sys.platform.startswith("win"):
            raise RuntimeError("solo Windows")
        import ctypes
        from ctypes import wintypes

        class BLOB(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]
        self.ct, self.BLOB = ctypes, BLOB
        self.crypt32, self.kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
        P = ctypes.POINTER(BLOB)
        self.crypt32.CryptProtectData.argtypes = [P, wintypes.LPCWSTR, P, ctypes.c_void_p, ctypes.c_void_p,
                                                  wintypes.DWORD, P]
        self.crypt32.CryptProtectData.restype = wintypes.BOOL
        self.crypt32.CryptUnprotectData.argtypes = [P, ctypes.c_void_p, P, ctypes.c_void_p, ctypes.c_void_p,
                                                    wintypes.DWORD, P]
        self.crypt32.CryptUnprotectData.restype = wintypes.BOOL
        self.kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        self.kernel32.LocalFree.restype = ctypes.c_void_p
        super().__init__()
        if self._unprotect(self._protect("prueba")) != "prueba":      # autoprueba: si falla, se usa otro método
            raise RuntimeError("DPAPI no funcionó")

    def _blob(self, data):
        buf = self.ct.create_string_buffer(data, len(data))
        return self.BLOB(len(data), self.ct.cast(buf, self.ct.POINTER(self.ct.c_char))), buf

    def _call(self, fn, data, *extra):
        src, keep1 = self._blob(data)
        ent, keep2 = self._blob(b"EspyX-Bot")
        out = self.BLOB()
        ok = fn(self.ct.byref(src), *extra, self.ct.byref(ent), None, None, 0x01, self.ct.byref(out))
        if not ok:
            raise OSError("DPAPI falló")
        try:
            return self.ct.string_at(out.pbData, out.cbData)
        finally:
            self.kernel32.LocalFree(self.ct.cast(out.pbData, self.ct.c_void_p))

    def _protect(self, pw):
        return base64.b64encode(self._call(self.crypt32.CryptProtectData, pw.encode("utf-8"), "EspyX")).decode("ascii")

    def _unprotect(self, blob):
        return self._call(self.crypt32.CryptUnprotectData, base64.b64decode(blob), None).decode("utf-8")


class _MacKeychain:
    name = "el Llavero de macOS"
    secure = True

    def __init__(self):
        if sys.platform != "darwin":
            raise RuntimeError("solo macOS")
        subprocess.run(["security", "help"], capture_output=True, timeout=5)

    @staticmethod
    def _q(text):
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'

    def save(self, email, pw):
        # La contraseña va por la entrada estándar del modo interactivo (`security -i`), no como argumento:
        # así no aparece en la lista de procesos mientras se guarda.
        cmd = f"add-generic-password -U -a {self._q(email)} -s {self._q(SERVICE)} -w {self._q(pw)}\n"
        subprocess.run(["security", "-i"], input=cmd.encode("utf-8"), capture_output=True, timeout=10)
        if self.load(email) == pw:
            return
        # si este macOS interpreta distinto las comillas, se guarda por argumento y se verifica igual
        r = subprocess.run(["security", "add-generic-password", "-U", "-a", email, "-s", SERVICE, "-w", pw],
                           capture_output=True, timeout=10)
        if r.returncode != 0 or self.load(email) != pw:
            raise OSError(r.stderr.decode(errors="ignore").strip() or "el Llavero rechazó la contraseña")

    def load(self, email):
        r = subprocess.run(["security", "find-generic-password", "-a", email, "-s", SERVICE, "-w"],
                           capture_output=True, timeout=10)
        return r.stdout.decode("utf-8").rstrip("\n") if r.returncode == 0 else None

    def delete(self, email):
        subprocess.run(["security", "delete-generic-password", "-a", email, "-s", SERVICE], capture_output=True,
                       timeout=10)


class _Obfuscated(_FileBackend):
    name = "ofuscada en tu carpeta de usuario (este sistema no ofrece cifrado; no es seguridad fuerte)"
    secure = False

    def _key(self, n):
        seed = (os.path.expanduser("~") + "|" + (os.environ.get("USER") or os.environ.get("USERNAME") or "")).encode()
        out = b""
        while len(out) < n:
            seed = hashlib.sha256(seed).digest()
            out += seed
        return out[:n]

    def _protect(self, pw):
        raw = pw.encode("utf-8")
        return base64.b64encode(bytes(a ^ b for a, b in zip(raw, self._key(len(raw))))).decode("ascii")

    def _unprotect(self, blob):
        raw = base64.b64decode(blob)
        return bytes(a ^ b for a, b in zip(raw, self._key(len(raw)))).decode("utf-8")


class SecureStore:
    def __init__(self):
        self.backend = None
        for cls in (_Keyring, _DPAPI, _MacKeychain, _Obfuscated):
            try:
                self.backend = cls()
                break
            except Exception:
                continue

    @property
    def available(self):
        return self.backend is not None

    @property
    def secure(self):
        return bool(self.backend and self.backend.secure)

    def describe(self):
        return self.backend.name if self.backend else "no disponible"

    def save(self, email, pw):
        if not self.backend:
            return False
        try:
            self.backend.save(email, pw)
            return True
        except Exception:
            return False

    def load(self, email):
        if not self.backend or not email:
            return None
        try:
            return self.backend.load(email)
        except Exception:
            return None

    def delete(self, email):
        if self.backend and email:
            try:
                self.backend.delete(email)
            except Exception:
                pass
