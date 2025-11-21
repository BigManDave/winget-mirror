#!/bin/bash
# Generate self-signed SSL certificates for winget mirror
# Run this script from the docker directory

set -e

SSL_DIR="./ssl"
CERT_FILE="$SSL_DIR/winget-mirror.crt"
KEY_FILE="$SSL_DIR/winget-mirror.key"

# Create SSL directory if it doesn't exist
mkdir -p "$SSL_DIR"

# Check if certificates already exist
if [ -f "$CERT_FILE" ] && [ -f "$KEY_FILE" ]; then
    echo "SSL certificates already exist at $SSL_DIR"
    echo "Remove them first if you want to regenerate:"
    echo "  rm -rf $SSL_DIR"
    exit 0
fi

echo "Generating self-signed SSL certificates..."

# Generate private key
openssl genrsa -out "$KEY_FILE" 2048

# Generate certificate
openssl req -new -x509 -key "$KEY_FILE" -out "$CERT_FILE" -days 365 -subj "/C=US/ST=State/L=City/O=Winget Mirror/CN=winget-mirror.local"

# Set proper permissions
chmod 600 "$KEY_FILE"
chmod 644 "$CERT_FILE"

echo "SSL certificates generated successfully!"
echo "Certificate: $CERT_FILE"
echo "Private Key: $KEY_FILE"
echo ""
echo "For production use, replace these with proper certificates from a CA."
echo "You can also update the CN (Common Name) to match your domain."