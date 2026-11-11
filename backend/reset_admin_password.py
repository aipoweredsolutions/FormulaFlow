from getpass import getpass

from sqlalchemy import func, select, update

from backend.database import (
    AdminAccount,
    AdminSession,
    SessionLocal,
    create_db_tables,
)
from backend.main import hash_admin_password


def main() -> None:
    email = input("Admin email: ").strip().lower()
    if not email:
        raise SystemExit("Admin email is required.")

    new_password = getpass("New password (12-128 characters): ")
    confirmation = getpass("Confirm new password: ")
    if len(new_password) < 12 or len(new_password) > 128:
        raise SystemExit("Password must be between 12 and 128 characters.")
    if new_password != confirmation:
        raise SystemExit("Passwords do not match.")

    create_db_tables()
    with SessionLocal.begin() as db:
        account = db.scalar(
            select(AdminAccount).where(
                AdminAccount.email == email,
                AdminAccount.role == "admin",
            )
        )
        if account is None:
            raise SystemExit("No admin account exists with that email.")

        account.password_hash = hash_admin_password(new_password)
        db.execute(
            update(AdminSession)
            .where(
                AdminSession.account_id == account.id,
                AdminSession.revoked_at.is_(None),
            )
            .values(revoked_at=func.current_timestamp())
        )

    print(f"Password updated for {email}. Existing sessions were signed out.")


if __name__ == "__main__":
    main()
