import sys

from app.auth import set_password
from app.db import SessionLocal
from app.models import User
from app.password_reset_service import invalidate_reset_tokens


def main() -> None:
    if len(sys.argv) != 3:
        print("Usage: python scripts/set_user_password.py <username> <password>")
        raise SystemExit(1)

    username = sys.argv[1]
    password = sys.argv[2]

    db = SessionLocal()

    try:
        user = db.query(User).filter(User.username == username).first()

        if not user:
            print(f"User not found: {username}")
            raise SystemExit(1)

        set_password(user, password)
        invalidate_reset_tokens(db, user.id)
        user.is_active = True

        db.commit()

        print(f"Password updated for user: {username}")

    finally:
        db.close()


if __name__ == "__main__":
    main()
