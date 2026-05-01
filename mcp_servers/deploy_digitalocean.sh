#!/bin/bash
# Deployment script for Digital Ocean

echo "🌊 Deploying MCP Servers to Digital Ocean"
echo "========================================="

# Configuration
DROPLET_IP=${1:-"your-droplet-ip"}
SSH_USER=${2:-"root"}
REPO_URL=${3:-"https://github.com/your-username/mcp-servers.git"}

if [ "$DROPLET_IP" = "your-droplet-ip" ]; then
    echo "❌ Please provide your droplet IP as the first argument"
    echo "Usage: ./deploy_digitalocean.sh <droplet-ip> [ssh-user] [repo-url]"
    exit 1
fi

echo "📡 Connecting to droplet: $DROPLET_IP"
echo "👤 SSH User: $SSH_USER"
echo "📦 Repository: $REPO_URL"

# Create deployment script for remote execution
cat > remote_deploy.sh << 'EOF'
#!/bin/bash
set -e

echo "🔧 Setting up MCP Servers on Digital Ocean..."

# Update system
apt-get update
apt-get upgrade -y

# Install Docker
curl -fsSL https://get.docker.com -o get-docker.sh
sh get-docker.sh
usermod -aG docker $USER

# Install Docker Compose
curl -L "https://github.com/docker/compose/releases/latest/download/docker-compose-$(uname -s)-$(uname -m)" -o /usr/local/bin/docker-compose
chmod +x /usr/local/bin/docker-compose

# Install Git
apt-get install -y git

# Clone repository
if [ -d "mcp_servers" ]; then
    cd mcp_servers
    git pull
else
    git clone REPO_URL_PLACEHOLDER mcp_servers
    cd mcp_servers
fi

# Set up environment
cp env.example .env
echo "⚠️  Please edit .env file with your production configuration:"
echo "   nano .env"

# Create systemd service for auto-start
cat > /etc/systemd/system/mcp_servers.service << 'SERVICE_EOF'
[Unit]
Description=MCP Servers
Requires=docker.service
After=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=/root/mcp_servers
ExecStart=/usr/local/bin/docker-compose up -d
ExecStop=/usr/local/bin/docker-compose down
TimeoutStartSec=0

[Install]
WantedBy=multi-user.target
SERVICE_EOF

# Enable service
systemctl enable mcp_servers.service

echo "✅ Setup complete!"
echo "📝 Next steps:"
echo "   1. Edit .env file: nano .env"
echo "   2. Start services: docker-compose up -d"
echo "   3. Check status: docker-compose ps"
echo "   4. View logs: docker-compose logs -f"
EOF

# Replace placeholder with actual repo URL
sed -i "s|REPO_URL_PLACEHOLDER|$REPO_URL|g" remote_deploy.sh

# Copy and execute on remote server
echo "📤 Uploading deployment script..."
scp remote_deploy.sh $SSH_USER@$DROPLET_IP:/tmp/

echo "🚀 Executing deployment on remote server..."
ssh $SSH_USER@$DROPLET_IP "chmod +x /tmp/remote_deploy.sh && /tmp/remote_deploy.sh"

# Clean up
rm remote_deploy.sh

echo ""
echo "🎉 Deployment initiated!"
echo "📋 Next steps:"
echo "   1. SSH into your droplet: ssh $SSH_USER@$DROPLET_IP"
echo "   2. Navigate to: cd mcp_servers"
echo "   3. Edit configuration: nano .env"
echo "   4. Start services: docker-compose up -d"
echo "   5. Check status: docker-compose ps"
echo ""
echo "🌐 Your MCP servers will be available at:"
echo "   - MCP List Service: http://$DROPLET_IP:8000"
echo "   - Supabase Server: http://$DROPLET_IP:8002"
echo "   - Timescale Server: http://$DROPLET_IP:8003"
echo "   - Jira Server: http://$DROPLET_IP:8004"
echo "   - VRM Server: http://$DROPLET_IP:8005"
