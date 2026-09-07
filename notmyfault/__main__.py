from notmyfault.application_paths import ApplicationPaths
from notmyfault.config import SignedConfigStore
from notmyfault.host.app import run


if __name__ == "__main__":
    run(store=SignedConfigStore(ApplicationPaths.default()))
