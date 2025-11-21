# Winget Mirror - Docker NGINX Setup

This directory contains a complete Docker Compose setup for hosting your winget mirror using NGINX with HTTPS support.

## Features

- **Latest NGINX**: Uses nginx:1.27-alpine (latest stable)
- **HTTPS Support**: Automatic redirect to HTTPS with self-signed certificates
- **Security Hardened**: Modern security practices and headers
- **Performance Optimized**: Gzip compression, caching, and rate limiting
- **Winget Compatible**: Proper CORS headers and directory indexing for manifests

## Quick Start

1. **Generate SSL certificates** (run from this directory):
   ```bash
   ./generate-ssl.sh
   ```

2. **Start the NGINX server**:
   ```bash
   docker-compose up -d
   ```

3. **Patch your manifests** (from the project root):
   ```bash
   cd ../test-mirror
   invoke patch-repo --server-url="https://localhost" --output-dir="./patched-manifests"
   ```

4. **Access your mirror**:
   - Manifests: https://localhost/manifests/
   - Downloads: https://localhost/downloads/
   - Health check: https://localhost/health

## Configuration

### SSL Certificates

The setup includes self-signed certificates for development. For production:

1. Replace `ssl/winget-mirror.crt` and `ssl/winget-mirror.key` with your CA-signed certificates
2. Update the `server_name` in `nginx.conf` to match your domain
3. Consider using certbot for Let's Encrypt certificates

### File Paths

The docker-compose.yml mounts:
- `./nginx.conf` → Container's nginx config
- `./ssl/` → SSL certificates
- `../test-mirror/patched-manifests/` → Patched manifests
- `../test-mirror/downloads/` → Downloaded installers

Update these paths if your mirror directory is different.

### Security Features

- **Rate Limiting**: 10 req/s for manifests, 5 req/s for downloads
- **Security Headers**: HSTS, XSS protection, content type sniffing prevention
- **No Directory Listing**: Disabled for downloads (security)
- **Hidden Files**: Blocked access to dotfiles
- **TLS 1.2/1.3 Only**: Modern SSL protocols
- **No Server Tokens**: Hides NGINX version

### Performance Features

- **Gzip Compression**: Enabled for text-based content
- **Caching**: 1 hour for manifests, 1 year for downloads
- **HTTP/2**: Enabled for better performance
- **Keepalive**: Optimized connection handling

## Winget Client Setup

After starting the server, configure winget to use your mirror:

```bash
# Add your mirror as a source
winget source add --name "Local Mirror" --arg "https://localhost/manifests"

# List available sources
winget source list

# Search for packages (should use your mirror)
winget search notepad
```

## Troubleshooting

### SSL Certificate Warnings

Since we're using self-signed certificates, browsers will show security warnings. For development, you can:
- Click "Advanced" → "Proceed to localhost (unsafe)" in Chrome
- Add the certificate to your system's trusted certificates

### Permission Issues

If you get permission errors:
```bash
# Fix permissions on SSL files
chmod 600 ssl/winget-mirror.key
chmod 644 ssl/winget-mirror.crt
```

### Port Conflicts

If ports 80/443 are in use:
- Change the ports in `docker-compose.yml`: `"8080:80" "8443:443"`
- Update your winget source URL accordingly

### File Not Found Errors

Ensure your patched manifests exist:
```bash
ls -la ../test-mirror/patched-manifests/manifests/
ls -la ../test-mirror/downloads/
```

## Production Deployment

For production use:

1. **Use proper SSL certificates** from a trusted CA
2. **Update server_name** in nginx.conf to your domain
3. **Enable firewall** and restrict access if needed
4. **Set up monitoring** for the NGINX logs
5. **Consider using a reverse proxy** like Traefik for additional features

## Logs

View NGINX logs:
```bash
docker-compose logs -f nginx
```

Logs are also available inside the container at `/var/log/nginx/`.