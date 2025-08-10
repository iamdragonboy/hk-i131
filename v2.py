import random
import logging
import subprocess
import sys
import os
import re
import time
import concurrent.futures
import discord
from discord.ext import commands, tasks
import docker
import asyncio
from discord import app_commands
from discord.ui import Button, View, Select
import string
from datetime import datetime, timedelta
from typing import Optional, Literal

TOKEN = ''
RAM_LIMIT = '96g'
SERVER_LIMIT = 10
database_file = 'database.txt'
PUBLIC_IP = '138.68.79.95'

# Admin user IDs - add your admin user IDs here
ADMIN_IDS = [1294649116575535124]  # Replace with actual admin IDs

intents = discord.Intents.default()
intents.messages = False
intents.message_content = False

bot = commands.Bot(command_prefix='/', intents=intents)
client = docker.from_env()

# Helper functions
def is_admin(user_id):
    return user_id in ADMIN_IDS

def generate_random_string(length=8):
    return ''.join(random.choices(string.ascii_letters + string.digits, k=length))

def generate_random_port(): 
    return random.randint(1025, 65535)

def parse_time_to_seconds(time_str):
    """Convert time string like '1d', '2h', '30m', '45s', '1y', '3M' to seconds"""
    if not time_str:
        return None
    
    units = {
        's': 1,               # seconds
        'm': 60,              # minutes
        'h': 3600,            # hours
        'd': 86400,           # days
        'M': 2592000,         # months (30 days)
        'y': 31536000         # years (365 days)
    }
    
    unit = time_str[-1]
    if unit in units and time_str[:-1].isdigit():
        return int(time_str[:-1]) * units[unit]
    elif time_str.isdigit():
        return int(time_str) * 86400  # Default to days if no unit specified
    return None

def format_expiry_date(seconds_from_now):
    """Convert seconds from now to a formatted date string"""
    if not seconds_from_now:
        return None
    
    expiry_date = datetime.now() + timedelta(seconds=seconds_from_now)
    return expiry_date.strftime("%Y-%m-%d %H:%M:%S")

def add_to_database(user, container_name, ssh_command, ram_limit=None, cpu_limit=None, creator=None, expiry=None, os_type="Ubuntu 22.04", hostname=None):
    with open(database_file, 'a') as f:
        f.write(f"{user}|{container_name}|{ssh_command}|{ram_limit or '2048'}|{cpu_limit or '1'}|{creator or user}|{os_type}|{expiry or 'None'}|{hostname or 'None'}\n")

def remove_from_database(container_id):
    if not os.path.exists(database_file):
        return
    with open(database_file, 'r') as f:
        lines = f.readlines()
    with open(database_file, 'w') as f:
        for line in lines:
            if container_id not in line:
                f.write(line)

def get_all_containers():
    if not os.path.exists(database_file):
        return []
    with open(database_file, 'r') as f:
        return [line.strip() for line in f.readlines()]

def get_container_stats(container_id):
    try:
        # Get memory usage
        mem_stats = subprocess.check_output(["docker", "stats", container_id, "--no-stream", "--format", "{{.MemUsage}}"]).decode().strip()
        
        # Get CPU usage
        cpu_stats = subprocess.check_output(["docker", "stats", container_id, "--no-stream", "--format", "{{.CPUPerc}}"]).decode().strip()
        
        # Get container status
        status = subprocess.check_output(["docker", "inspect", "--format", "{{.State.Status}}", container_id]).decode().strip()
        
        return {
            "memory": mem_stats,
            "cpu": cpu_stats,
            "status": "🟢 Online" if status == "running" else "🔴 Offline"
        }
    except Exception:
        return {"memory": "N/A", "cpu": "N/A", "status": "🔴 Offline"}

def get_system_stats():
    try:
        # Get total memory usage
        total_mem = subprocess.check_output(["free", "-m"]).decode().strip()
        mem_lines = total_mem.split('\n')
        if len(mem_lines) >= 2:
            mem_values = mem_lines[1].split()
            total_mem = mem_values[1]
            used_mem = mem_values[2]
            
        return {
            "total_memory": f"{total_mem}MB",
            "used_memory": f"{used_mem}MB"
        }
    except Exception as e:
        return {
            "total_memory": "N/A",
            "used_memory": "N/A",
            "error": str(e)
        }

async def capture_ssh_session_line(process):
    while True:
        output = await process.stdout.readline()
        if not output:
            break
        output = output.decode('utf-8').strip()
        if "ssh session:" in output:
            return output.split("ssh session:")[1].strip()
    return None

def get_ssh_command_from_database(container_id):
    if not os.path.exists(database_file):
        return None
    with open(database_file, 'r') as f:
        for line in f:
            if container_id in line:
                parts = line.strip().split('|')
                if len(parts) >= 3:
                    return parts[2]
    return None

def get_user_servers(user):
    if not os.path.exists(database_file):
        return []
    servers = []
    with open(database_file, 'r') as f:
        for line in f:
            if line.startswith(user):
                servers.append(line.strip())
    return servers

def count_user_servers(user):
    return len(get_user_servers(user))

def get_container_id_from_database(user, container_name=None):
    servers = get_user_servers(user)
    if servers:
        if container_name:
            for server in servers:
                parts = server.split('|')
                if len(parts) >= 2 and container_name in parts[1]:
                    return parts[1]
            return None
        else:
            return servers[0].split('|')[1]
    return None

# OS Selection dropdown for deploy and create-vps commands
class OSSelectView(View):
    def __init__(self, callback):
        super().__init__(timeout=60)
        self.callback = callback
        
        select = Select(
            placeholder="Select an operating system",
            options=[
                discord.SelectOption(label="Ubuntu 22.04", description="Latest LTS Ubuntu release", emoji="🐧", value="ubuntu"),
                discord.SelectOption(label="Debian 12", description="Stable Debian release", emoji="🐧", value="debian")
            ]
        )
        
        select.callback = self.select_callback
        self.add_item(select)
        
    async def select_callback(self, interaction: discord.Interaction):
        selected_os = interaction.data["values"][0]
        await interaction.response.defer()
        await self.callback(interaction, selected_os)

# Confirmation dialog class for delete operations
class ConfirmView(View):
    def __init__(self, container_id, container_name, is_delete_all=False):
        super().__init__(timeout=60)
        self.container_id = container_id
        self.container_name = container_name
        self.is_delete_all = is_delete_all
        
    @discord.ui.button(label="✅ Confirm", style=discord.ButtonStyle.danger)
    async def confirm_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=False)
        
        try:
            if self.is_delete_all:
                containers = get_all_containers()
                deleted_count = 0
                
                for container_info in containers:
                    parts = container_info.split('|')
                    if len(parts) >= 2:
                        container_id = parts[1]
                        try:
                            subprocess.run(["docker", "stop", container_id], check=True, stderr=subprocess.DEVNULL)
                            subprocess.run(["docker", "rm", container_id], check=True, stderr=subprocess.DEVNULL)
                            deleted_count += 1
                        except Exception:
                            pass
                
                with open(database_file, 'w') as f:
                    f.write('')
                    
                embed = discord.Embed(
                    title="All VPS Instances Deleted",
                    description=f"Successfully deleted {deleted_count} VPS instances.",
                    color=0x00ff00
                )
                await interaction.followup.send(embed=embed)
                
                for child in self.children:
                    child.disabled = True
                
            else:
                try:
                    subprocess.run(["docker", "stop", self.container_id], check=True, stderr=subprocess.DEVNULL)
                    subprocess.run(["docker", "rm", self.container_id], check=True, stderr=subprocess.DEVNULL)
                    remove_from_database(self.container_id)
                    
                    embed = discord.Embed(
                        title="VPS Deleted",
                        description=f"Successfully deleted VPS instance `{self.container_name}`.",
                        color=0x00ff00
                    )
                    await interaction.followup.send(embed=embed)
                    
                    for child in self.children:
                        child.disabled = True
                    
                except Exception as e:
                    embed = discord.Embed(
                        title="❌ Error",
                        description=f"Failed to delete VPS instance: {str(e)}",
                        color=0xff0000
                    )
                    await interaction.followup.send(embed=embed)
        except Exception as e:
            try:
                await interaction.followup.send(f"An error occurred: {str(e)}")
            except:
                pass
    
    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=False)
        
        embed = discord.Embed(
            title="🚫 Operation Cancelled",
            description="The delete operation has been cancelled.",
            color=0xffaa00
        )
        await interaction.followup.send(embed=embed)
        
        for child in self.children:
            child.disabled = True

@bot.event
async def on_ready():
    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name="Gamerhacker"))
    await bot.tree.sync()
    print(f"✅ Logged in as {bot.user}")

@tasks.loop(seconds=5)
async def change_status():
    try:
        if os.path.exists(database_file):
            with open(database_file, 'r') as f:
                lines = f.readlines()
                instance_count = len(lines)
        else:
            instance_count = 0

        status = f"with {instance_count} Cloud Instances 🌐"
        await bot.change_presence(activity=discord.Game(name=status))
    except Exception as e:
        print(f"Failed to update status: {e}")

@bot.tree.command(name="nodedmin", description="📊 Admin: Lists all VPSs, their details, and SSH commands")
async def nodedmin(interaction: discord.Interaction):
    if not is_admin(interaction.user.id):
        embed = discord.Embed(
            title="❌ Access Denied",
            description="You don't have permission to use this command.",
            color=0xff0000
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    await interaction.response.defer()

    if not os.path.exists(database_file):
        embed = discord.Embed(
            title="VPS Instances",
            description="No VPS data available.",
            color=0xff0000
        )
        await interaction.followup.send(embed=embed)
        return

    embed = discord.Embed(
        title="All VPS Instances",
        description="Detailed information about all VPS instances",
        color=0x00aaff
    )
    
    with open(database_file, 'r') as f:
        lines = f.readlines()
    
    embeds = []
    current_embed = embed
    field_count = 0
    
    for line in lines:
        parts = line.strip().split('|')
        
        if field_count >= 25:
            embeds.append(current_embed)
            current_embed = discord.Embed(
                title="📊 All VPS Instances (Continued)",
                description="Detailed information about all VPS instances",
                color=0x00aaff
            )
            field_count = 0
        
        if len(parts) >= 9:
            user, container_name, ssh_command, ram, cpu, creator, os_type, expiry, hostname = parts
            stats = get_container_stats(container_name)
            
            current_embed.add_field(
                name=f"🖥️ {container_name} ({stats['status']})",
                value=f"🪩 **User:** {user}\n"
                      f"💾 **RAM:** {ram}GB\n"
                      f"🔥 **CPU:** {cpu} cores\n"
                      f"🌐 **OS:** {os_type}\n"
                      f"👑 **Creator:** {creator}\n"
                      f"🏷️ **Hostname:** {hostname}\n"
                      f"🔑 **SSH:** `{ssh_command}`",
                inline=False
            )
            field_count += 1
        elif len(parts) >= 3:
            user, container_name, ssh_command = parts
            stats = get_container_stats(container_name)
            
            current_embed.add_field(
                name=f"🖥️ {container_name} ({stats['status']})",
                value=f"👤 **User:** {user}\n"
                      f"🔑 **SSH:** `{ssh_command}`",
                inline=False
            )
            field_count += 1
    
    if field_count > 0:
        embeds.append(current_embed)
    
    if not embeds:
        await interaction.followup.send("No VPS instances found.")
        return
        
    for i, embed in enumerate(embeds):
        await interaction.followup.send(embed=embed)

@bot.tree.command(name="node", description="☠️ Shows system resource usage and VPS status for all users")
async def node_stats(interaction: discord.Interaction):
    await interaction.response.defer()
    
    system_stats = get_system_stats()
    containers = get_all_containers()
    
    embed = discord.Embed(
        title="🖥️ System Resource Usage",
        description="Current resource usage and VPS status",
        color=0x00aaff
    )
    
    embed.add_field(
        name="🔥 Memory Usage",
        value=f"Used: {system_stats['used_memory']} / Total: {system_stats['total_memory']}",
        inline=False
    )
    
    embed.add_field(
        name=f"🧊 VPS Instances ({len(containers)})",
        value="List of all VPS instances, their status, and resource usage:",
        inline=False
    )
    
    for container_info in containers:
        parts = container_info.split('|')
        if len(parts) >= 9:
            user, container_id, _, ram, cpu, _, os_type, expiry, hostname = parts
            stats = get_container_stats(container_id)
            embed.add_field(
                name=f"{container_id} ({stats['status']})",
                value=f"👤 **User:** {user}\n"
                      f"💾 **RAM:** {stats['memory']}\n"
                      f"🔥 **CPU:** {stats['cpu']}\n"
                      f"🌐 **OS:** {os_type}\n"
                      f"🏷️ **Hostname:** {hostname}\n"
                      f"⏱️ **Expires:** {expiry}",
                inline=True
            )
        elif len(parts) >= 2:
            user, container_id = parts[:2]
            stats = get_container_stats(container_id)
            embed.add_field(
                name=f"{container_id} ({stats['status']})",
                value=f"👤 **User:** {user}\n"
                      f"💾 **RAM:** {stats['memory']}\n"
                      f"🔥 **CPU:** {stats['cpu']}",
                inline=True
            )
    
    await interaction.followup.send(embed=embed)

async def regen_ssh_command(interaction: discord.Interaction, container_name: str):
    user = str(interaction.user)
    container_id = get_container_id_from_database(user, container_name)

    if not container_id:
        embed = discord.Embed(
            title="❌ Not Found",
            description="No active instance found with that name for your user.",
            color=0xff0000
        )
        await interaction.response.send_message(embed=embed)
        return

    try:
        exec_cmd = await asyncio.create_subprocess_exec("docker", "exec", container_id, "tmate", "-F",
                                                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    except subprocess.CalledProcessError as e:
        embed = discord.Embed(
            title="❌ Error",
            description=f"Error executing tmate in Docker container: {e}",
            color=0xff0000
        )
        await interaction.response.send_message(embed=embed)
        return

    ssh_session_line = await capture_ssh_session_line(exec_cmd)
    if ssh_session_line:
        if os.path.exists(database_file):
            with open(database_file, 'r') as f:
                lines = f.readlines()
            with open(database_file, 'w') as f:
                for line in lines:
                    if container_id in line:
                        parts = line.strip().split('|')
                        if len(parts) >= 3:
                            parts[2] = ssh_session_line
                            f.write('|'.join(parts) + '\n')
                    else:
                        f.write(line)
        
        dm_embed = discord.Embed(
            title="🔄 New SSH Session Generated",
            description="Your SSH session has been regenerated successfully.",
            color=0x00ff00
        )
        dm_embed.add_field(
            name="🔑 SSH Connection Command",
            value=f"```{ssh_session_line}```",
            inline=False
        )
        await interaction.user.send(embed=dm_embed)
        
        success_embed = discord.Embed(
            title="✅ SSH Session Regenerated",
            description="New SSH session generated. Check your DMs for details.",
            color=0x00ff00
        )
        await interaction.response.send_message(embed=success_embed)
    else:
        error_embed = discord.Embed(
            title="❌ Failed",
            description="Failed to generate new SSH session.",
            color=0xff0000
        )
        await interaction.response.send_message(embed=error_embed)

async def start_server(interaction: discord.Interaction, container_name: str):
    user = str(interaction.user)
    container_id = get_container_id_from_database(user, container_name)

    if not container_id:
        embed = discord.Embed(
            title="❌ Not Found",
            description="No instance found with that name for your user.",
            color=0xff0000
        )
        await interaction.response.send_message(embed=embed)
        return

    await interaction.response.defer()

    try:
        subprocess.run(["docker", "start", container_id], check=True)
        exec_cmd = await asyncio.create_subprocess_exec("docker", "exec", container_id, "tmate", "-F",
                                                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        ssh_session_line = await capture_ssh_session_line(exec_cmd)
        
        if ssh_session_line:
            if os.path.exists(database_file):
                with open(database_file, 'r') as f:
                    lines = f.readlines()
                with open(database_file, 'w') as f:
                    for line in lines:
                        if container_id in line:
                            parts = line.strip().split('|')
                            if len(parts) >= 3:
                                parts[2] = ssh_session_line
                                f.write('|'.join(parts) + '\n')
                        else:
                            f.write(line)
            
            dm_embed = discord.Embed(
                title="▶️ VPS Started",
                description=f"Your VPS instance `{container_name}` has been started successfully.",
                color=0x00ff00
            )
            dm_embed.add_field(
                name="🔑 SSH Connection Command",
                value=f"```{ssh_session_line}```",
                inline=False
            )
            
            try:
                await interaction.user.send(embed=dm_embed)
                
                success_embed = discord.Embed(
                    title="✅ VPS Started",
                    description=f"Your VPS instance `{container_name}` has been started. Check your DMs for connection details.",
                    color=0x00ff00
                )
                await interaction.followup.send(embed=success_embed)
            except discord.Forbidden:
                warning_embed = discord.Embed(
                    title="⚠️ Cannot Send DM",
                    description="Your VPS has been started, but I couldn't send you a DM with the connection details. Please enable DMs from server members.",
                    color=0xffaa00
                )
                warning_embed.add_field(
                    name="🔑 SSH Connection Command",
                    value=f"```{ssh_session_line}```",
                    inline=False
                )
                await interaction.followup.send(embed=warning_embed)
        else:
            error_embed = discord.Embed(
                title="⚠️ Partial Success",
                description="VPS started, but failed to get SSH session line.",
                color=0xffaa00
            )
            await interaction.followup.send(embed=error_embed)
    except subprocess.CalledProcessError as e:
        error_embed = discord.Embed(
            title="❌ Error",
            description=f"Error starting VPS instance: {e}",
            color=0xff0000
        )
        await interaction.followup.send(embed=error_embed)

async def stop_server(interaction: discord.Interaction, container_name: str):
    user = str(interaction.user)
    container_id = get_container_id_from_database(user, container_name)

    if not container_id:
        embed = discord.Embed(
            title="❌ Not Found",
            description="No instance found with that name for your user.",
            color=0xff0000
        )
        await interaction.response.send_message(embed=embed)
        return

    await interaction.response.defer()

    try:
        subprocess.run(["docker", "stop", container_id], check=True)
        success_embed = discord.Embed(
            title="⏹️ VPS Stopped",
            description=f"Your VPS instance `{container_name}` has been stopped. You can start it again with `/start {container_name}`",
            color=0x00ff00
        )
        await interaction.followup.send(embed=success_embed)
    except subprocess.CalledProcessError as e:
        error_embed = discord.Embed(
            title="❌ Error",
            description=f"Failed to stop VPS instance: {str(e)}",
            color=0xff0000
        )
        await interaction.followup.send(embed=error_embed)

async def restart_server(interaction: discord.Interaction, container_name: str):
    user = str(interaction.user)
    container_id = get_container_id_from_database(user, container_name)

    if not container_id:
        embed = discord.Embed(
            title="❌ Not Found",
            description="No instance found with that name for your user.",
            color=0xff0000
        )
        await interaction.response.send_message(embed=embed)
        return

    await interaction.response.defer()

    try:
        subprocess.run(["docker", "restart", container_id], check=True)
        exec_cmd = await asyncio.create_subprocess_exec("docker", "exec", container_id, "tmate", "-F",
                                                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        ssh_session_line = await capture_ssh_session_line(exec_cmd)
        
        if ssh_session_line:
            if os.path.exists(database_file):
                with open(database_file, 'r') as f:
                    lines = f.readlines()
                with open(database_file, 'w') as f:
                    for line in lines:
                        if container_id in line:
                            parts = line.strip().split('|')
                            if len(parts) >= 3:
                                parts[2] = ssh_session_line
                                f.write('|'.join(parts) + '\n')
                        else:
                            f.write(line)
            
            dm_embed = discord.Embed(
                title="🔄 VPS Restarted",
                description=f"Your VPS instance `{container_name}` has been restarted successfully.",
                color=0x00ff00
            )
            dm_embed.add_field(
                name="🔑 SSH Connection Command",
                value=f"```{ssh_session_line}```",
                inline=False
            )
            
            try:
                await interaction.user.send(embed=dm_embed)
                
                success_embed = discord.Embed(
                    title="✅ VPS Restarted",
                    description=f"Your VPS instance `{container_name}` has been restarted. Check your DMs for connection details.",
                    color=0x00ff00
                )
                await interaction.followup.send(embed=success_embed)
            except discord.Forbidden:
                warning_embed = discord.Embed(
                    title="⚠️ Cannot Send DM",
                    description="Your VPS has been restarted, but I couldn't send you a DM with the connection details. Please enable DMs from server members.",
                    color=0xffaa00
                )
                warning_embed.add_field(
                    name="🔑 SSH Connection Command",
                    value=f"```{ssh_session_line}```",
                    inline=False
                )
                await interaction.followup.send(embed=warning_embed)
        else:
            error_embed = discord.Embed(
                title="⚠️ Partial Success",
                description="VPS restarted, but failed to get SSH session line.",
                color=0xffaa00
            )
            await interaction.followup.send(embed=error_embed)
    except subprocess.CalledProcessError as e:
        error_embed = discord.Embed(
            title="❌ Error",
            description=f"Error restarting VPS instance: {e}",
            color=0xff0000
        )
        await interaction.followup.send(embed=error_embed)

async def capture_output(process, keyword):
    while True:
        output = await process.stdout.readline()
        if not output:
            break
        output = output.decode('utf-8').strip()
        if keyword in output:
            return output
    return None

@bot.tree.command(name="port-add", description="🔌 Adds a port forwarding rule")
@app_commands.describe(container_name="The name of the container", container_port="The port in the container")
async def port_add(interaction: discord.Interaction, container_name: str, container_port: int):
    user_id = str(interaction.user.id)
    if not has_access(user_id, container_name):
        embed = discord.Embed(
            title="❌ Access Denied",
            description="You don't have access to this VPS.",
            color=0xff0000
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    embed = discord.Embed(
        title="🔄 Setting Up Port Forwarding",
        description="Setting up port forwarding. This might take a moment...",
        color=0x00aaff
    )
    await interaction.response.send_message(embed=embed)

    public_port = generate_random_port()

    command = f"ssh -o StrictHostKeyChecking=no -R {public_port}:localhost:{container_port} serveo.net -N -f"

    try:
        await asyncio.create_subprocess_exec(
            "docker", "exec", container_name, "bash", "-c", command,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL
        )

        success_embed = discord.Embed(
            title="✅ Port Forwarding Successful",
            description=f"Your service is now accessible from the internet.",
            color=0x00ff00
        )
        success_embed.add_field(
            name="🌐 Connection Details",
            value=f"**Host:** {PUBLIC_IP}\n**Port:** {public_port}",
            inline=False
        )
        await interaction.followup.send(embed=success_embed)

    except Exception as e:
        error_embed = discord.Embed(
            title="❌ Error",
            description=f"An unexpected error occurred: {e}",
            color=0xff0000
        )
        await interaction.followup.send(embed=error_embed)

@bot.tree.command(name="addport", description="🔌 Installs port forwarding tools in your VPS")
@app_commands.describe(container_name="The name of your container")
async def addport(interaction: discord.Interaction, container_name: str):
    user_id = str(interaction.user.id)
    if not has_access(user_id, container_name):
        embed = discord.Embed(
            title="❌ Access Denied",
            description="You don't have access to this VPS.",
            color=0xff0000
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    embed = discord.Embed(
        title="🔄 Installing Port Forwarding Tools",
        description="Installing necessary tools for port forwarding (curl, ca-certificates, etc.). This might take a moment...",
        color=0x00aaff
    )
    await interaction.response.send_message(embed=embed)

    try:
        command = "apt update || true && apt install curl -y && apt install --reinstall ca-certificates -y && update-ca-certificates && bash <(curl -fsSL https://raw.githubusercontent.com/steeldevlol/port/refs/heads/main/install)"
        await asyncio.create_subprocess_exec(
            "docker", "exec", container_name, "bash", "-c", command,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL
        )

        success_embed = discord.Embed(
            title="✅ Installation Successful",
            description="Port forwarding tools have been installed in your VPS.",
            color=0x00ff00
        )
        await interaction.followup.send(embed=success_embed)

    except Exception as e:
        error_embed = discord.Embed(
            title="❌ Error",
            description=f"Failed to install port forwarding tools: {e}",
            color=0xff0000
        )
        await interaction.followup.send(embed=error_embed)

@bot.tree.command(name="port-http", description="🌐 Forward HTTP traffic to your container")
@app_commands.describe(container_name="The name of your container", container_port="The port inside the container to forward")
async def port_forward_website(interaction: discord.Interaction, container_name: str, container_port: int):
    user_id = str(interaction.user.id)
    if not has_access(user_id, container_name):
        embed = discord.Embed(
            title="❌ Access Denied",
            description="You don't have access to this VPS.",
            color=0xff0000
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    embed = discord.Embed(
        title="🔄 Setting Up HTTP Forwarding",
        description="Setting up HTTP forwarding. This might take a moment...",
        color=0x00aaff
    )
    await interaction.response.send_message(embed=embed)
    
    try:
        exec_cmd = await asyncio.create_subprocess_exec(
            "docker", "exec", container_name, "ssh", "-o", "StrictHostKeyChecking=no", "-R", f"80:localhost:{container_port}", "serveo.net",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        url_line = await capture_output(exec_cmd, "Forwarding HTTP traffic from")
        
        if url_line:
            url = url_line.split(" ")[-1]
            success_embed = discord.Embed(
                title="✅ HTTP Forwarding Successful",
                description=f"Your web service is now accessible from the internet.",
                color=0x00ff00
            )
            success_embed.add_field(
                name="🌐 Website URL",
                value=f"[{url}](https://{url})",
                inline=False
            )
            await interaction.followup.send(embed=success_embed)
        else:
            error_embed = discord.Embed(
                title="❌ Error",
                description="Failed to set up HTTP forwarding. Please try again later.",
                color=0xff0000
            )
            await interaction.followup.send(embed=error_embed)
    except Exception as e:
        error_embed = discord.Embed(
            title="❌ Error",
            description=f"An unexpected error occurred: {e}",
            color=0xff0000
        )
        await interaction.followup.send(embed=error_embed)

@bot.tree.command(name="deploy", description="🚀 Admin: Deploy a new VPS instance")
@app_commands.describe(
    ram="RAM allocation in GB (max 96gb)",
    cpu="CPU cores (max 12)",
    target_user="Discord user ID to assign the VPS to",
    container_name="Custom container name (default: auto-generated)",
    expiry="Time until expiry (e.g. 1d, 2h, 30m, 45s, 1y, 3M)",
    hostname="Custom hostname for the VPS"
)
async def deploy(
    interaction: discord.Interaction, 
    ram: int = 16, 
    cpu: int = 4, 
    target_user: str = None,
    container_name: str = None,
    expiry: str = None,
    hostname: str = None
):
    if interaction.user.id not in ADMIN_IDS:
        embed = discord.Embed(
            title="❌ Access Denied",
            description="You don't have permission to use this command.",
            color=0xff0000
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    
    if ram > 96000:
        ram = 96000
    if cpu > 12:
        cpu = 12
    
    user_id = target_user if target_user else str(interaction.user.id)
    user = target_user if target_user else str(interaction.user)
    
    if not container_name:
        username = interaction.user.name.replace(" ", "_")
        random_string = generate_random_string(8)
        container_name = f"VPS_{username}_{random_string}"
    
    expiry_seconds = parse_time_to_seconds(expiry)
    expiry_date = format_expiry_date(expiry_seconds) if expiry_seconds else None
    
    embed = discord.Embed(
        title="🖥️ Select Operating System",
        description="🔍 Please select the operating system for your VPS instance",
        color=0x00aaff
    )
    
    async def os_selected_callback(interaction, selected_os):
        await deploy_with_os(interaction, selected_os, ram, cpu, user_id, user, container_name, expiry_date, hostname)
    
    view = OSSelectView(os_selected_callback)
    await interaction.response.send_message(embed=embed, view=view)

async def deploy_with_os(interaction, os_type, ram, cpu, user_id, user, container_name, expiry_date, hostname=None):
    embed = discord.Embed(
        title="🛠️ Creating VPS",
        description=f"💾 **RAM:** {ram}GB\n"
                    f"🔥 **CPU:** {cpu} cores\n"
                    f"🧊 **Container Name:** {container_name}\n"
                    f"🏷️ **Hostname:** {hostname if hostname else 'Default'}",
        color=0x00ff00
    )
    await interaction.followup.send(embed=embed)
    
    image = get_docker_image_for_os(os_type)
    
    try:
        container_args = [
            "docker", "run", "-itd", 
            "--privileged", 
            "--cap-add=ALL",
            f"--memory={ram}g",
            f"--cpus={cpu}",
            "--name", container_name
        ]
        if hostname:
            container_args.extend(["--hostname", hostname])
        container_args.append(image)
        
        container_id = subprocess.check_output(container_args).strip().decode('utf-8')
    except subprocess.CalledProcessError as e:
        error_embed = discord.Embed(
            title="❌ Error",
            description=f"Error creating Docker container: {e}",
            color=0xff0000
        )
        await interaction.followup.send(embed=error_embed)
        return

    try:
        exec_cmd = await asyncio.create_subprocess_exec("docker", "exec", container_name, "tmate", "-F",
                                                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    except Exception as e:
        error_embed = discord.Embed(
            title="❌ Error",
            description=f"Error executing tmate in Docker container: {e}",
            color=0xff0000
        )
        await interaction.followup.send(embed=error_embed)
        
        subprocess.run(["docker", "stop", container_name], check=False)
        subprocess.run(["docker", "rm", container_name], check=False)
        return

    ssh_session_line = await capture_ssh_session_line(exec_cmd)
    if ssh_session_line:
        add_to_database(
            user, 
            container_name, 
            ssh_session_line, 
            ram_limit=ram, 
            cpu_limit=cpu,
            creator=str(interaction.user),
            expiry=expiry_date,
            os_type=os_type_to_display_name(os_type),
            hostname=hostname
        )
        
        dm_embed = discord.Embed(
            title="✅ VPS Created Successfully",
            description=f"Your VPS instance `{container_name}` has been created.",
            color=0x00ff00
        )
        dm_embed.add_field(name="🔑 SSH Connection Command", value=f"```{ssh_session_line}```", inline=False)
        dm_embed.add_field(name="💾 RAM Allocation", value=f"{ram}GB", inline=True)
        dm_embed.add_field(name="🔥 CPU Cores", value=f"{cpu} cores", inline=True)
        dm_embed.add_field(name="🏷️ Hostname", value=hostname if hostname else "Default", inline=True)
        dm_embed.add_field(name="🔒 Password", value="hk-i9", inline=True)
        dm_embed.set_footer(text="Keep this information safe and private!")
        
        try:
            target_user_obj = await bot.fetch_user(int(user_id))
            await target_user_obj.send(embed=dm_embed)
            
            success_embed = discord.Embed(
                title="✅ VPS Created Successfully",
                description=f"VPS instance has been created for <@{user_id}>. They should check their DMs for connection details.",
                color=0x00ff00
            )
            await interaction.followup.send(embed=success_embed)
            
        except discord.Forbidden:
            warning_embed = discord.Embed(
                title="⚠️ Cannot Send DM",
                description=f"VPS has been created, but I couldn't send a DM with the connection details to <@{user_id}>. Please enable DMs from server members.",
                color=0xffaa00
            )
            warning_embed.add_field(name="🔑 SSH Connection Command", value=f"```{ssh_session_line}```", inline=False)
            await interaction.followup.send(embed=warning_embed)
    else:
        try:
            subprocess.run(["docker", "stop", container_name], check=False)
            subprocess.run(["docker", "rm", container_name], check=False)
        except Exception:
            pass
        
        error_embed = discord.Embed(
            title="❌ Deployment Failed",
            description="Failed to establish SSH session. The container has been cleaned up. Please try again.",
            color=0xff0000
        )
        await interaction.followup.send(embed=error_embed)

@bot.tree.command(name="create-vps", description="🚀 Admin: Create a new VPS instance with custom specs")
@app_commands.describe(
    ram="RAM allocation in GB (max 96gb)",
    cpu="CPU cores (max 12)",
    target_user="Discord user to assign the VPS to",
    expiry="Time until expiry (e.g. 1d, 2h, 30m, 45s, 1y, 3M)",
    hostname="Custom hostname for the VPS"
)
async def create_vps(
    interaction: discord.Interaction, 
    ram: int = 16, 
    cpu: int = 4,
    target_user: discord.User = None,
    expiry: str = None,
    hostname: str = None
):
    if interaction.user.id not in ADMIN_IDS:
        embed = discord.Embed(
            title="❌ Access Denied",
            description="You don't have permission to use this command.",
            color=0xff0000
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    
    if ram > 96000:
        ram = 96000
    if cpu > 12:
        cpu = 12
    
    user_id = str(target_user.id) if target_user else str(interaction.user.id)
    user = str(target_user) if target_user else str(interaction.user)
    
    username = (target_user.name if target_user else interaction.user.name).replace(" ", "_")
    random_string = generate_random_string(8)
    container_name = f"root_{username}_{random_string}"
    
    expiry_seconds = parse_time_to_seconds(expiry)
    expiry_date = format_expiry_date(expiry_seconds) if expiry_seconds else None
    
    embed = discord.Embed(
        title="🖥️ Select Operating System",
        description="🔍 Please select the operating system for your VPS instance",
        color=0x00aaff
    )
    
    async def os_selected_callback(interaction, selected_os):
        await deploy_with_os(interaction, selected_os, ram, cpu, user_id, user, container_name, expiry_date, hostname)
    
    view = OSSelectView(os_selected_callback)
    await interaction.response.send_message(embed=embed, view=view)

@bot.tree.command(name="send_vps", description="📤 Admin: Send VPS SSH and password to a user")
@app_commands.describe(
    container_name="The name of the VPS container",
    target_user="The user to send the VPS details to"
)
async def send_vps(interaction: discord.Interaction, container_name: str, target_user: discord.User):
    if interaction.user.id not in ADMIN_IDS:
        embed = discord.Embed(
            title="❌ Access Denied",
            description="You don't have permission to use this command.",
            color=0xff0000
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    
    container_id = container_name
    ssh_command = get_ssh_command_from_database(container_id)
    
    if not ssh_command:
        embed = discord.Embed(
            title="❌ VPS Not Found",
            description=f"No VPS found with the name `{container_name}`.",
            color=0xff0000
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    
    dm_embed = discord.Embed(
        title="✅ VPS Details",
        description=f"VPS instance `{container_name}` details.",
        color=0x00ff00
    )
    dm_embed.add_field(name="🔑 SSH Connection Command", value=f"```{ssh_command}```", inline=False)
    dm_embed.add_field(name="🔒 Password", value="hk-i9", inline=True)
    dm_embed.set_footer(text="Keep this information safe and private!")
    
    try:
        await target_user.send(embed=dm_embed)
        
        success_embed = discord.Embed(
            title="✅ VPS Details Sent",
            description=f"VPS details for `{container_name}` have been sent to {target_user.mention}.",
            color=0x00ff00
        )
        await interaction.response.send_message(embed=success_embed)
        
    except discord.Forbidden:
        warning_embed = discord.Embed(
            title="⚠️ Cannot Send DM",
            description=f"Could not send VPS details to {target_user.mention}. Please ensure their DMs are enabled.",
            color=0xffaa00
        )
        await interaction.response.send_message(embed=warning_embed)

@bot.tree.command(name="vpspanel", description="🌐 Access your VPS web panel")
@app_commands.describe(container_name="The name of your container")
async def vpspanel(interaction: discord.Interaction, container_name: str):
    user_id = str(interaction.user.id)
    if not has_access(user_id, container_name):
        embed = discord.Embed(
            title="❌ Access Denied",
            description="You don't have access to this VPS.",
            color=0xff0000
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    embed = discord.Embed(
        title="🔄 Setting Up VPS Web Panel",
        description="Setting up web panel access. This might take a moment...",
        color=0x00aaff
    )
    await interaction.response.send_message(embed=embed)
    
    try:
        exec_cmd = await asyncio.create_subprocess_exec(
            "docker", "exec", container_name, "ssh", "-o", "StrictHostKeyChecking=no", "-R", "80:localhost:80", "serveo.net",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        url_line = await capture_output(exec_cmd, "Forwarding HTTP traffic from")
        
        if url_line:
            url = url_line.split(" ")[-1]
            success_embed = discord.Embed(
                title="✅ Web Panel Access",
                description=f"Your VPS web panel is now accessible.",
                color=0x00ff00
            )
            success_embed.add_field(
                name="🌐 Web Panel URL",
                value=f"[{url}](https://{url})",
                inline=False
            )
            await interaction.followup.send(embed=success_embed)
        else:
            error_embed = discord.Embed(
                title="❌ Error",
                description="Failed to set up web panel access. Please ensure a web server is running on port 80 in your VPS.",
                color=0xff0000
            )
            await interaction.followup.send(embed=error_embed)
    except Exception as e:
        error_embed = discord.Embed(
            title="❌ Error",
            description=f"An unexpected error occurred: {e}",
            color=0xff0000
        )
        await interaction.followup.send(embed=error_embed)

def os_type_to_display_name(os_type):
    os_map = {
        "ubuntu": "Ubuntu 22.04",
        "debian": "Debian 12"
    }
    return os_map.get(os_type, "Unknown OS")

def get_docker_image_for_os(os_type):
    os_map = {
        "ubuntu": "ubuntu-22.04-with-tmate",
        "debian": "debian-with-tmate"
    }
    return os_map.get(os_type, "ubuntu-22.04-with-tmate")

class TipsView(View):
    def __init__(self):
        super().__init__(timeout=300)
        self.current_page = 0
        self.tips = [
            {
                "title": "🔑 SSH Connection Tips",
                "description": "• Use `ssh-keygen` to create SSH keys for passwordless login\n"
                              "• Forward ports with `-L` flag: `ssh -L 8080:localhost:80 user@host`\n"
                              "• Keep connections alive with `ServerAliveInterval=60` in SSH config\n"
                              "• Use `tmux` or `screen` to keep sessions running after disconnect"
            },
            {
                "title": "🛠️ System Management",
                "description": "• Update packages regularly: `apt update && apt upgrade`\n"
                              "• Monitor resources with `htop` or `top`\n"
                              "• View logs with `journalctl` or check `/var/log/`"
            },
            {
                "title": "🌐 Web Hosting Tips",
                "description": "• Install Nginx or Apache for web hosting\n"
                              "• Secure with Let's Encrypt for free SSL certificates\n"
                              "• Use PM2 to manage Node.js applications\n"
                              "• Set up proper firewall rules with `ufw`"
            },
            {
                "title": "📊 Performance Optimization",
                "description": "• Limit resource-intensive processes\n"
                              "• Use caching for web applications\n"
                              "• Configure swap space for low-memory situations\n"
                              "• Optimize database queries and indexes"
            },
            {
                "title": "🔒 Security Best Practices",
                "description": "• Change default passwords immediately\n"
                              "• Disable root SSH login\n"
                              "• Keep software updated\n"
                              "• Use `fail2ban` to prevent brute force attacks\n"
                              "• Regularly backup important data"
            }
        ]
    
    @discord.ui.button(label="◀️ Previous", style=discord.ButtonStyle.secondary)
    async def previous_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page = (self.current_page - 1) % len(self.tips)
        await interaction.response.edit_message(embed=self.get_current_embed(), view=self)
    
    @discord.ui.button(label="▶️ Next", style=discord.ButtonStyle.primary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page = (self.current_page + 1) % len(self.tips)
        await interaction.response.edit_message(embed=self.get_current_embed(), view=self)
    
    def get_current_embed(self):
        tip = self.tips[self.current_page]
        embed = discord.Embed(
            title=tip["title"],
            description=tip["description"],
            color=0x00aaff
        )
        embed.set_footer(text=f"Tip {self.current_page + 1}/{len(self.tips)}")
        return embed

@bot.tree.command(name="tips", description="💡 Shows useful tips for managing your VPS")
async def tips_command(interaction: discord.Interaction):
    view = TipsView()
    embed = view.get_current_embed()
    await interaction.response.send_message(embed=embed, view=view)

@bot.tree.command(name="delete", description="Delete your VPS instance")
@app_commands.describe(container_name="The name of your container")
async def delete_server(interaction: discord.Interaction, container_name: str):
    user = str(interaction.user)
    container_id = get_container_id_from_database(user, container_name)

    if not container_id:
        embed = discord.Embed(
            title="❌ Not Found",
            description="No instance found with that name for your user.",
            color=0xff0000
        )
        await interaction.response.send_message(embed=embed)
        return

    confirm_embed = discord.Embed(
        title="⚠️ Confirm Deletion",
        description=f"Are you sure you want to delete VPS instance `{container_name}`? This action cannot be undone.",
        color=0xffaa00
    )
    
    view = ConfirmView(container_id, container_name)
    await interaction.response.send_message(embed=confirm_embed, view=view)

@bot.tree.command(name="delete-all", description="🗑️ Admin: Delete all VPS instances")
async def delete_all_servers(interaction: discord.Interaction):
    if interaction.user.id not in ADMIN_IDS:
        embed = discord.Embed(
            title="❌ Access Denied",
            description="You don't have permission to use this command.",
            color=0xff0000
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    
    containers = get_all_containers()
    
    confirm_embed = discord.Embed(
        title="⚠️ Confirm Mass Deletion",
        description=f"Are you sure you want to delete ALL {len(containers)} VPS instances? This action cannot be undone.",
        color=0xffaa00
    )
    
    view = ConfirmView(None, None, is_delete_all=True)
    await interaction.response.send_message(embed=confirm_embed, view=view)

@bot.tree.command(name="list", description="📋 List all your VPS instances")
async def list_servers(interaction: discord.Interaction):
    user = str(interaction.user)
    servers = get_user_servers(user)

    await interaction.response.defer()

    if not servers:
        embed = discord.Embed(
            title="📋 Your VPS Instances",
            description="You don't have any VPS instances. Use `/deploy` to create one!",
            color=0x00aaff
        )
        await interaction.followup.send(embed=embed)
        return

    embed = discord.Embed(
        title="📋 Your VPS Instances",
        description=f"You have {len(servers)} VPS instance(s)",
        color=0x00aaff
    )

    for server in servers:
        parts = server.split('|')
        container_id = parts[1]
        
        try:
            container_info = subprocess.check_output(["docker", "inspect", "--format", "{{.State.Status}}", container_id]).decode().strip()
            status = "🟢 Online" if container_info == "running" else "🔴 Offline"
        except:
            status = "🔴 Offline"
        
        if len(parts) >= 9:
            ram_limit, cpu_limit, creator, os_type, expiry, hostname = parts[3], parts[4], parts[5], parts[6], parts[7], parts[8]
            
            embed.add_field(
                name=f"🖥️ {container_id} ({status})",
                value=f"💾 **RAM:** {ram_limit}GB\n"
                      f"🔥 **CPU:** {cpu_limit} cores\n"
                      f"🧊 **OS:** {os_type}\n"
                      f"👑 **Created by:** {creator}\n"
                      f"🏷️ **Hostname:** {hostname}\n"
                      f"⏱️ **Expires:** {expiry}",
                inline=False
            )
        else:
            embed.add_field(
                name=f"🖥️ {container_id} ({status})",
                value=f"💾 **RAM:** 16GB\n"
                      f"🔥 **CPU:** 4 cores\n"
                      f"🧊 **OS:** Ubuntu 22.04",
                inline=False
            )

    await interaction.followup.send(embed=embed)

@bot.tree.command(name="regen-ssh", description="🔄 Regenerate SSH session for your instance")
@app_commands.describe(container_name="The name of your container")
async def regen_ssh(interaction: discord.Interaction, container_name: str):
    await regen_ssh_command(interaction, container_name)

@bot.tree.command(name="start", description="▶️ Start your VPS instance")
@app_commands.describe(container_name="The name of your container")
async def start(interaction: discord.Interaction, container_name: str):
    await start_server(interaction, container_name)

@bot.tree.command(name="stop", description="⏹️ Stop your VPS instance")
@app_commands.describe(container_name="The name of your container")
async def stop(interaction: discord.Interaction, container_name: str):
    await stop_server(interaction, container_name)

@bot.tree.command(name="restart", description="🔄 Restart your VPS instance")
@app_commands.describe(container_name="The name of your container")
async def restart(interaction: discord.Interaction, container_name: str):
    await restart_server(interaction, container_name)

@bot.tree.command(name="ping", description="🏓 Check the bot's latency")
async def ping(interaction: discord.Interaction):
    latency = round(bot.latency * 1000)
    embed = discord.Embed(
        title="🏓 Pong!",
        description=f"Latency: {latency}ms",
        color=0x00ff00
    )
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="help", description="❓ Shows the help message")
async def help_command(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🌟 VPS Bot Help",
        description="Here are all the available commands:",
        color=0x00aaff
    )
    
    embed.add_field(
        name="📋 User Commands",
        value="Commands available to all users:",
        inline=False
    )
    embed.add_field(name="/start <container_name>", value="Start your VPS instance", inline=True)
    embed.add_field(name="/stop <container_name>", value="Stop your VPS instance", inline=True)
    embed.add_field(name="/restart <container_name>", value="Restart your VPS instance", inline=True)
    embed.add_field(name="/regen-ssh <container_name>", value="Regenerate SSH credentials", inline=True)
    embed.add_field(name="/list", value="List all your VPS instances", inline=True)
    embed.add_field(name="/delete <container_name>", value="Delete your VPS instance", inline=True)
    embed.add_field(name="/port-add <container_name> <port>", value="Forward a port", inline=True)
    embed.add_field(name="/addport <container_name>", value="Install port forwarding tools", inline=True)
    embed.add_field(name="/port-http <container_name> <port>", value="Forward HTTP traffic", inline=True)
    embed.add_field(name="/vpspanel <container_name>", value="Access your VPS web panel", inline=True)
    embed.add_field(name="/ping", value="Check bot latency", inline=True)
    embed.add_field(name="/create", value="Claim a VPS reward by invite or boost", inline=True)
    embed.add_field(name="/manage <container_name>", value="Manage your VPS using control panel", inline=True)
    embed.add_field(name="/sharevps <container_name> <target_user>", value="Share VPS access with another user", inline=True)
    embed.add_field(name="/myshares", value="List all users you've shared VPS access with", inline=True)
    embed.add_field(name="/revokeshareall <container_name>", value="Remove all shared access from a VPS", inline=True)
    embed.add_field(name="/node", value="View system resource usage and VPS status", inline=True)
    
    if interaction.user.id in ADMIN_IDS:
        embed.add_field(
            name="👑 Admin Commands",
            value="Commands available only to admins:",
            inline=False
        )
        embed.add_field(name="/deploy", value="Deploy a new VPS with custom settings", inline=True)
        embed.add_field(name="/create-vps", value="Create a new VPS with custom specs", inline=True)
        embed.add_field(name="/send_vps <container_name> <target_user>", value="Send VPS SSH and password to a user", inline=True)
        embed.add_field(name="/nodedmin", value="List all VPS instances with details", inline=True)
        embed.add_field(name="/delete-all", value="Delete all VPS instances", inline=True)
        embed.add_field(name="/sharesof <userid>", value="Check who has access to someone’s VPS", inline=True)
    
    await interaction.response.send_message(embed=embed)

ACCESS_FILE = "access.txt"
SHARE_LIMIT = 3

def get_shared_users(container_name):
    if not os.path.exists(ACCESS_FILE):
        return []
    with open(ACCESS_FILE, 'r') as f:
        return [line.split('|')[1].strip() for line in f if line.startswith(container_name + "|")]

def add_shared_user(container_name, user_id):
    if not os.path.exists(ACCESS_FILE):
        with open(ACCESS_FILE, 'w'): pass
    users = get_shared_users(container_name)
    if str(user_id) not in users and len(users) < SHARE_LIMIT:
        with open(ACCESS_FILE, 'a') as f:
            f.write(f"{container_name}|{user_id}\n")

def remove_shared_user(container_name, user_id):
    if not os.path.exists(ACCESS_FILE):
        return
    with open(ACCESS_FILE, 'r') as f:
        lines = f.readlines()
    with open(ACCESS_FILE, 'w') as f:
        for line in lines:
            if not (line.startswith(container_name + "|") and str(user_id) in line):
                f.write(line)

def has_access(user_id, container_name):
    if not os.path.exists(database_file):
        return False
    with open(database_file, 'r') as f:
        for line in f:
            parts = line.strip().split('|')
            if len(parts) >= 2 and parts[1] == container_name and parts[0] == str(user_id):
                return True
    if not os.path.exists(ACCESS_FILE):
        return False
    with open(ACCESS_FILE, 'r') as f:
        for line in f:
            if line.startswith(container_name + "|") and str(user_id) in line:
                return True
    return False

@bot.tree.command(name="sharevps", description="🤝 Share VPS access with another user")
@app_commands.describe(container_name="The name of your container", target_user="The user to share access with")
async def share_vps(interaction: discord.Interaction, container_name: str, target_user: discord.User):
    user_id = str(interaction.user.id)
    if not has_access(user_id, container_name):
        embed = discord.Embed(
            title="❌ Access Denied",
            description="You don't have access to this VPS.",
            color=0xff0000
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    shared_users = get_shared_users(container_name)
    if len(shared_users) >= SHARE_LIMIT:
        embed = discord.Embed(
            title="❌ Share Limit Reached",
            description=f"You can only share this VPS with up to {SHARE_LIMIT} users.",
            color=0xff0000
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    if str(target_user.id) in shared_users:
        embed = discord.Embed(
            title="❌ Already Shared",
            description=f"This VPS is already shared with {target_user.mention}.",
            color=0xff0000
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    add_shared_user(container_name, target_user.id)
    ssh_command = get_ssh_command_from_database(container_name)
    
    dm_embed = discord.Embed(
        title="✅ VPS Access Shared",
        description=f"You have been granted access to VPS instance `{container_name}`.",
        color=0x00ff00
    )
    dm_embed.add_field(name="🔑 SSH Connection Command", value=f"```{ssh_command}```", inline=False)
    dm_embed.add_field(name="🔒 Password", value="hk-i9", inline=True)
    dm_embed.set_footer(text="Keep this information safe and private!")
    
    try:
        await target_user.send(embed=dm_embed)
        
        success_embed = discord.Embed(
            title="✅ VPS Access Shared",
            description=f"VPS access for `{container_name}` has been shared with {target_user.mention}.",
            color=0x00ff00
        )
        await interaction.response.send_message(embed=success_embed)
        
    except discord.Forbidden:
        warning_embed = discord.Embed(
            title="⚠️ Cannot Send DM",
            description=f"VPS access has been shared, but I couldn't send a DM to {target_user.mention}. Please ensure their DMs are enabled.",
            color=0xffaa00
        )
        await interaction.response.send_message(embed=warning_embed)

@bot.tree.command(name="myshares", description="📋 List all users you've shared VPS access with")
async def my_shares(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
    servers = get_user_servers(user_id)
    embed = discord.Embed(
        title="📋 Your Shared VPS Access",
        description="List of all users you've shared your VPS instances with",
        color=0x00aaff
    )
    
    found_shares = False
    for server in servers:
        parts = server.split('|')
        container_name = parts[1]
        shared_users = get_shared_users(container_name)
        if shared_users:
            found_shares = True
            user_mentions = []
            for user_id in shared_users:
                try:
                    user = await bot.fetch_user(int(user_id))
                    user_mentions.append(user.mention)
                except:
                    user_mentions.append(f"User ID: {user_id}")
            embed.add_field(
                name=f"🖥️ {container_name}",
                value=f"Shared with: {', '.join(user_mentions)}",
                inline=False
            )
    
    if not found_shares:
        embed.add_field(
            name="No Shares",
            value="You haven't shared any VPS instances with other users.",
            inline=False
        )
    
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="revokeshareall", description="🚫 Revoke all shared access from a VPS")
@app_commands.describe(container_name="The name of your container")
async def revoke_share_all(interaction: discord.Interaction, container_name: str):
    user_id = str(interaction.user.id)
    if not has_access(user_id, container_name):
        embed = discord.Embed(
            title="❌ Access Denied",
            description="You don't have access to this VPS.",
            color=0xff0000
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    shared_users = get_shared_users(container_name)
    if not shared_users:
        embed = discord.Embed(
            title="❌ No Shares",
            description=f"No users have shared access to `{container_name}`.",
            color=0xff0000
        )
        await interaction.response.send_message(embed=embed)
        return

    confirm_embed = discord.Embed(
        title="⚠️ Confirm Revoke All Shares",
        description=f"Are you sure you want to revoke access for ALL users from `{container_name}`? This action cannot be undone.",
        color=0xffaa00
    )
    
    class RevokeConfirmView(View):
        def __init__(self):
            super().__init__(timeout=60)
        
        @discord.ui.button(label="✅ Confirm", style=discord.ButtonStyle.danger)
        async def confirm_revoke_button(self, interaction: discord.Interaction, button: discord.ui.Button):
            await interaction.response.defer(ephemeral=False)
            
            try:
                if os.path.exists(ACCESS_FILE):
                    with open(ACCESS_FILE, 'r') as f:
                        lines = f.readlines()
                    with open(ACCESS_FILE, 'w') as f:
                        for line in lines:
                            if not line.startswith(container_name + "|"):
                                f.write(line)
                
                success_embed = discord.Embed(
                    title="✅ All Shares Revoked",
                    description=f"All shared access for `{container_name}` has been revoked.",
                    color=0x00ff00
                )
                await interaction.followup.send(embed=success_embed)
                
                for child in self.children:
                    child.disabled = True
                
            except Exception as e:
                error_embed = discord.Embed(
                    title="❌ Error",
                    description=f"Failed to revoke shares: {str(e)}",
                    color=0xff0000
                )
                await interaction.followup.send(embed=error_embed)
        
        @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.secondary)
        async def cancel_revoke_button(self, interaction: discord.Interaction, button: discord.ui.Button):
            await interaction.response.defer(ephemeral=False)
            
            embed = discord.Embed(
                title="🚫 Operation Cancelled",
                description="The revoke operation has been cancelled.",
                color=0xffaa00
            )
            await interaction.followup.send(embed=embed)
            
            for child in self.children:
                child.disabled = True
    
    view = RevokeConfirmView()
    await interaction.response.send_message(embed=confirm_embed, view=view)

@bot.tree.command(name="sharesof", description="👀 Admin: Check who has access to a user's VPS")
@app_commands.describe(userid="The Discord user ID to check")
async def shares_of(interaction: discord.Interaction, userid: str):
    if interaction.user.id not in ADMIN_IDS:
        embed = discord.Embed(
            title="❌ Access Denied",
            description="You don't have permission to use this command.",
            color=0xff0000
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    
    servers = get_user_servers(userid)
    embed = discord.Embed(
        title=f"📋 Shared Access for User {userid}",
        description=f"List of VPS instances owned by <@{userid}> and their shared users",
        color=0x00aaff
    )
    
    found_shares = False
    for server in servers:
        parts = server.split('|')
        container_name = parts[1]
        shared_users = get_shared_users(container_name)
        if shared_users:
            found_shares = True
            user_mentions = []
            for user_id in shared_users:
                try:
                    user = await bot.fetch_user(int(user_id))
                    user_mentions.append(user.mention)
                except:
                    user_mentions.append(f"User ID: {user_id}")
            embed.add_field(
                name=f"🖥️ {container_name}",
                value=f"Shared with: {', '.join(user_mentions)}",
                inline=False
            )
    
    if not found_shares:
        embed.add_field(
            name="No Shares",
            value=f"User <@{userid}> has not shared any VPS instances with other users.",
            inline=False
        )
    
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="create", description="🎁 Claim a VPS reward by invite or boost")
async def create(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🎁 Claim Your VPS",
        description="To claim a VPS reward, please contact an admin or follow the server invite/boost reward program guidelines.",
        color=0x00aaff
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="manage", description="🛠️ Manage your VPS using a control panel")
@app_commands.describe(container_name="The name of your container")
async def manage(interaction: discord.Interaction, container_name: str):
    user_id = str(interaction.user.id)
    if not has_access(user_id, container_name):
        embed = discord.Embed(
            title="❌ Access Denied",
            description="You don't have access to this VPS.",
            color=0xff0000
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    embed = discord.Embed(
        title="🛠️ VPS Control Panel",
        description=f"Manage your VPS instance `{container_name}`",
        color=0x00aaff
    )
    
    class ManageView(View):
        def __init__(self):
            super().__init__(timeout=300)
        
        @discord.ui.button(label="▶️ Start", style=discord.ButtonStyle.green)
        async def start_button(self, interaction: discord.Interaction, button: discord.ui.Button):
            await start_server(interaction, container_name)
        
        @discord.ui.button(label="⏹️ Stop", style=discord.ButtonStyle.red)
        async def stop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
            await stop_server(interaction, container_name)
        
        @discord.ui.button(label="🔄 Restart", style=discord.ButtonStyle.blurple)
        async def restart_button(self, interaction: discord.Interaction, button: discord.ui.Button):
            await restart_server(interaction, container_name)
        
        @discord.ui.button(label="🔄 Regenerate SSH", style=discord.ButtonStyle.grey)
        async def regen_ssh_button(self, interaction: discord.Interaction, button: discord.ui.Button):
            await regen_ssh_command(interaction, container_name)
    
    view = ManageView()
    await interaction.response.send_message(embed=embed, view=view)

bot.run(TOKEN)
