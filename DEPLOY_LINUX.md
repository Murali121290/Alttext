# Linux Deployment Guide

This guide describes how to deploy the AltText application on a Linux server (e.g., Ubuntu/Debian) using Docker and Docker Compose.

## Prerequisites

Ensure your Linux server has the following installed:
- **Git**: To clone the repository.
- **Docker**: To run the application containers.
- **Docker Compose**: To orchestrate the application and database.

### 1. Update System and Install Docker

If you haven't installed Docker yet, run the following commands:

```bash
# Update package list
sudo apt update
sudo apt upgrade -y

# Install prerequisite packages
sudo apt install -y apt-transport-https ca-certificates curl software-properties-common git

# Add Docker's official GPG key
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /usr/share/keyrings/docker-archive-keyring.gpg

# Add Docker repository
echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/docker-archive-keyring.gpg] https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

# Install Docker Engine
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io

# Install Docker Compose (V2 is included in newer docker-ce, otherwise install plugin)
sudo apt install -y docker-compose-plugin

# Verify installation
sudo docker --version
sudo docker compose version
```

## Deployment Steps

### 1. Clone the Repository

Clone the project to your desired directory (e.g., `/opt/alttext` or `~/alttext`).

```bash
cd ~
git clone https://github.com/Murali121290/Alttext.git
cd Alttext
```

> **Note on Authentication:**
> If you see `Password authentication is not supported`, you need to use a **Personal Access Token (PAT)**.
> 1. Go to GitHub -> Settings -> Developer Settings -> Personal access tokens -> Tokens (classic).
> 2. Generate a new token with `repo` scope.
> 3. Use this token as your **Password** when prompted in the terminal.

### 2. Configure Environment Variables

Create a `.env` file in the root directory. You can copy the example if one exists, or create a new one.

```bash
nano .env
```

Add the following configuration (replace values with your actual keys):

```ini
# Gemini API Key (Required)
GEMINI_API_KEY

# Flask Secret Key (Generate a random string for production)
SECRET_KEY=your_strong_random_secret_string

# Database settings are handled automatically by Docker Compose,
# but can be overridden here if needed.
# DATABASE_URL=postgresql://postgres:postgres@db:5432/alttext
```

Save and exit (`Ctrl+O`, `Enter`, `Ctrl+X`).

### 3. Start the Application

Run the application using Docker Compose. This will build the Python image and start both the Web and Database containers.

```bash
sudo docker compose up -d --build
```

- `-d`: Detached mode (runs in background).
- `--build`: Rebuilds the images to ensure latest code is used.

### 4. Verify Deployment

Check if the containers are running:

```bash
sudo docker compose ps
```

You should see two services: `alttext_app` (or `web`) and `alttext_db` (or `db`) with status `Up`.

View logs to ensure there are no errors:

```bash
sudo docker compose logs -f web
```

### 5. Access the Application

The application is running on port **5000**.

-   **Browser:** `http://<your-server-ip>:5000`
-   **Default Admin Credentials:**
    -   Username: `admin`
    -   Password: `admin123` (Change this immediately after logging in!)

## Maintenance

### Updating the App

To update the application with the latest code from GitHub:

```bash
# 1. Pull latest changes
git pull origin main

# 2. Rebuild and restart containers
sudo docker compose up -d --build
```

### Stopping the App

```bash
sudo docker compose down
```

### Backups

To backup the PostgreSQL database:

```bash
sudo docker exec -t alttext_db pg_dumpall -c -U postgres > dump_`date +%d-%m-%Y"_"%H_%M_%S`.sql
```

## Production Setup: Nginx (Port 80/443)

You asked about `/var/www/html`. **You do NOT need to move your project files to `/var/www/html`**. 

With Docker, your application runs inside a container. However, for a production server, it is best practice to use **Nginx** as a "Reverse Proxy". This allows users to access your site via `http://your-domain.com` (Port 80) instead of `http://IP:5000`.

### 1. Install Nginx
```bash
sudo apt install -y nginx
```

### 2. Configure Nginx
Copy the provided configuration file to Nginx's sites-available directory:

```bash
# Copy config from repo
sudo cp nginx.conf /etc/nginx/sites-available/alttext

# Edit the file to set your domain/IP if needed
sudo nano /etc/nginx/sites-available/alttext
```

# 1. Pull the latest change
git pull origin main
# 2. Update the system-wide configuration
sudo cp nginx.conf /etc/nginx/sites-available/alttext
# 3. Reload Nginx
sudo systemctl reload nginx


### 3. Enable the Site
Link the file to `sites-enabled` and remove the default:

```bash
sudo ln -s /etc/nginx/sites-available/alttext /etc/nginx/sites-enabled/
sudo rm /etc/nginx/sites-enabled/default  # Optional: Removes default welcome page
```

### 4. Restart Nginx
```bash
sudo systemctl restart nginx
```

Now your application is accessible at `http://<your-ip>` (without port 5000). The application files remain safely in your home directory (e.g., `~/Alttext`), and Nginx forwards traffic to them.
