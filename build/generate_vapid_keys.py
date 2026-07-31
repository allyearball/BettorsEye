"""
One-time (or one-off, if you ever need to rotate keys) VAPID key generator for push
notifications. Run with:  python build/generate_vapid_keys.py

Prints two values:
  - VAPID_PUBLIC_KEY  -> paste into build/app_template.html as the VAPID_PUBLIC_KEY
                         JS constant near the top of the second <script> block.
  - VAPID_PRIVATE_KEY -> add as a GitHub Actions secret of the same name. Never commit
                         this value to the repo or share it anywhere.

Requires: pip install cryptography --break-system-packages
"""
import base64
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization


def main():
    priv = ec.generate_private_key(ec.SECP256R1())
    pub = priv.public_key()

    priv_val = priv.private_numbers().private_value
    priv_bytes = priv_val.to_bytes(32, 'big')
    priv_b64url = base64.urlsafe_b64encode(priv_bytes).decode().rstrip('=')

    pub_raw = pub.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    pub_b64url = base64.urlsafe_b64encode(pub_raw).decode().rstrip('=')

    print('VAPID_PUBLIC_KEY (paste into build/app_template.html, safe to be public):')
    print(pub_b64url)
    print()
    print('VAPID_PRIVATE_KEY (GitHub Actions secret only, never commit or share):')
    print(priv_b64url)


if __name__ == '__main__':
    main()
