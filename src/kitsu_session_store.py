"""
Keep the login details of the Kitsu Review plugin between OpenRV sessions.
"""

from kitsu_review_log import log

try:
    from PySide6 import QtCore
except ImportError:
    QtCore = None

# Optional: the OS credential store (macOS Keychain, Windows Credential
# Manager, Secret Service / KWallet on Linux). Without it, "Remember me" is
# unavailable. The app remembers only the server URL and the email.
try:
    import keyring
    import keyring.errors
except ImportError:
    keyring = None

SETTINGS_ORG = "KitsuReview"
SETTINGS_APP = "OpenRVPlugin"
KEYRING_SERVICE = "kitsu-review-openrv"
# Store the tokens as separate credential entries. Windows Credential Manager
# limits a single secret to about 1280 characters. Two JWTs together can be
# more than that.
TOKEN_KINDS = ("access_token", "refresh_token")


class SessionStore:
    """
    Save the login details between OpenRV sessions.

    The server URL, the email and the "Remember me" choice are not secret. They
    go in QSettings. The Kitsu session tokens go in the OS credential store
    through `keyring`.

    The app never stores the password. A login exchanges it for tokens, and the
    app reuses those tokens. Tokens expire on the server. Log Out revokes them.
    """

    def __init__(self):
        self._settings = QtCore.QSettings(SETTINGS_ORG, SETTINGS_APP)

    # -- non-secret settings -------------------------------------------

    def load_identity(self):
        server = self._settings.value("server", "", type=str) or ""
        email = self._settings.value("email", "", type=str) or ""
        return server, email

    def save_identity(self, server, email):
        self._settings.setValue("server", server)
        self._settings.setValue("email", email)
        self._settings.sync()

    def remember_enabled(self):
        return bool(self._settings.value("remember", True, type=bool))

    def set_remember_enabled(self, enabled):
        self._settings.setValue("remember", bool(enabled))
        self._settings.sync()

    # -- secrets (OS credential store) ---------------------------------

    @staticmethod
    def secure_available():
        """
        Return True if keyring is installed and has a real backend. Return
        False for the "fail" backend that keyring uses on, for example,
        headless Linux.
        """
        if keyring is None:
            return False
        try:
            return float(getattr(keyring.get_keyring(), "priority", 0)) > 0
        except Exception:
            return False

    @staticmethod
    def _account(server, email, kind):
        return f"{email} @ {server} [{kind}]"

    @staticmethod
    def _delete(account):
        try:
            keyring.delete_password(KEYRING_SERVICE, account)
        except keyring.errors.PasswordDeleteError:
            pass  # nothing is stored under that name

    def load_tokens(self, server, email):
        if not self.secure_available():
            return None
        tokens = {}
        for kind in TOKEN_KINDS:
            try:
                value = keyring.get_password(
                    KEYRING_SERVICE, self._account(server, email, kind)
                )
            except Exception as exc:
                log(
                    f"Could not read the saved session from the "
                    f"credential store: {exc}"
                )
                return None
            if value:
                tokens[kind] = value
        return tokens or None

    def save_tokens(self, server, email, tokens):
        if not self.secure_available():
            return False
        try:
            for kind in TOKEN_KINDS:
                account = self._account(server, email, kind)
                value = (tokens or {}).get(kind)
                if value:
                    keyring.set_password(KEYRING_SERVICE, account, str(value))
                else:
                    self._delete(account)
        except Exception as exc:
            log(f"Could not save the session to the credential store: {exc}")
            return False
        return True

    def clear_tokens(self, server, email):
        if not server or not email or not self.secure_available():
            return
        for kind in TOKEN_KINDS:
            try:
                self._delete(self._account(server, email, kind))
            except Exception as exc:
                log(f"Could not remove a saved session entry: {exc}")
