#!/bin/zsh
# One-time: create a self-signed code-signing certificate in your login keychain.
# Builds signed with it keep the same identity, so macOS keeps the Input Monitoring /
# Accessibility permissions across rebuilds. It is only valid on this Mac.
set -euo pipefail
NAME="DeathAdder V2 Local Signing"
OSSL=/usr/bin/openssl

if security find-certificate -c "$NAME" >/dev/null 2>&1; then
  echo "Certificate '$NAME' already exists."
  exit 0
fi

D=$(mktemp -d)
trap 'rm -rf "$D"' EXIT
cat > "$D/c.cnf" <<EOF
[req]
distinguished_name=dn
x509_extensions=ext
prompt=no
[dn]
CN=$NAME
[ext]
basicConstraints=critical,CA:false
keyUsage=critical,digitalSignature
extendedKeyUsage=critical,codeSigning
EOF
$OSSL req -x509 -newkey rsa:2048 -nodes -keyout "$D/k.pem" -out "$D/c.pem" -days 3650 -config "$D/c.cnf" 2>/dev/null
P=$($OSSL rand -hex 12)
# Apple's /usr/bin/openssl (LibreSSL) writes p12 files the keychain can import; Homebrew's OpenSSL 3+/4 may not
$OSSL pkcs12 -export -inkey "$D/k.pem" -in "$D/c.pem" -out "$D/c.p12" -passout "pass:$P"
security import "$D/c.p12" -k ~/Library/Keychains/login.keychain-db -P "$P" -T /usr/bin/codesign
echo "Created '$NAME'. Now run ./build.sh"
