import os
import io
import asyncio
import time
import discord
from discord.ext import commands
from discord.ui import View, Select, button
from dotenv import load_dotenv
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading

load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True

bot = commands.Bot(command_prefix='$', intents=intents)

# Main stock list
STOCK_LIST = []

# Tracks cooldowns: { user_id: timestamp_when_cooldown_ends }
USER_COOLDOWNS = {}

# Cooldown duration in seconds (50 minutes = 3000 seconds)
COOLDOWN_DURATION = 50 * 60  

# Tracks the channel and message objects for panels and stock messages
LAST_STOCK_MESSAGE = None
PANEL_MESSAGE = None
PANEL_CHANNEL = None


# --- Simple Web Server for Render Health Check ---
class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running!")

def run_server():
    server = HTTPServer(('0.0.0.0', 10000), SimpleHandler)
    server.serve_forever()


# --- Automatically Delete Command Messages (Prefix Only) ---
@bot.event
async def on_command_completion(ctx):
    """Deletes the user's message after a prefix command successfully runs."""
    if ctx.guild and ctx.message and not ctx.interaction:
        try:
            await ctx.message.delete()
        except discord.HTTPException:
            pass


# --- Permission Guard for Commands ---
@bot.check
async def check_admin_permissions(ctx):
    """Global check ensuring only Administrators can run commands."""
    if ctx.guild is None:
        return True
    
    if ctx.author.guild_permissions.administrator:
        return True
    
    if ctx.message and not ctx.interaction:
        try:
            await ctx.message.delete()
        except discord.HTTPException:
            pass

    await ctx.send("keep trying bud but not today", ephemeral=True)
    return False


async def send_stock_update(channel):
    """Deletes the previous stock message and sends a new updated message."""
    global LAST_STOCK_MESSAGE, PANEL_CHANNEL
    
    if channel:
        PANEL_CHANNEL = channel

    if PANEL_CHANNEL is None:
        return

    if LAST_STOCK_MESSAGE:
        try:
            await LAST_STOCK_MESSAGE.delete()
        except discord.HTTPException:
            pass

    count = len(STOCK_LIST)
    
    stock_embed = discord.Embed(
        description=f"📦 **Live Stock:** `{count}` items available",
        color=discord.Color.green() if count > 0 else discord.Color.red()
    )
    
    try:
        LAST_STOCK_MESSAGE = await PANEL_CHANNEL.send(embed=stock_embed)
    except discord.HTTPException:
        LAST_STOCK_MESSAGE = None


# --- Add Stock Menu ---
class AddStockSelect(Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Text Input", description="Add raw text in DM", emoji="📝"),
            discord.SelectOption(label="File Upload", description="Upload a file as 1 item in DM", emoji="📁"),
        ]
        super().__init__(placeholder="Select how to add stock...", options=options)

    async def callback(self, interaction: discord.Interaction):
        if interaction.user != self.view.author:
            await interaction.response.send_message("❌ This menu is not for you!", ephemeral=True)
            return

        def check_dm(m):
            return m.author == interaction.user and isinstance(m.channel, discord.DMChannel)

        choice = self.values[0]

        if choice == "Text Input":
            await interaction.response.send_message("📝 Please send the item text below:")
            try:
                msg = await bot.wait_for('message', check=check_dm, timeout=120.0)
                STOCK_LIST.append({"type": "text", "content": msg.content})
                await msg.reply(f"✅ Item added! Total in stock: `{len(STOCK_LIST)}`")
                await send_stock_update(None)
            except asyncio.TimeoutError:
                await interaction.followup.send("⏰ Timed out.")

        elif choice == "File Upload":
            instructions = (
                "📁 **How to upload your file:**\n"
                "1. Click the **`+`** icon next to your message typing box below.\n"
                "2. Click **Upload a File** to open your computer/phone file browser.\n"
                "3. Select your file and press **Enter** to send it here!"
            )
            await interaction.response.send_message(instructions)

            def check_file(m):
                return check_dm(m) and len(m.attachments) > 0

            try:
                msg = await bot.wait_for('message', check=check_file, timeout=120.0)
                attachment = msg.attachments[0]
                file_bytes = await attachment.read()

                STOCK_LIST.append({
                    "type": "file",
                    "filename": attachment.filename,
                    "content": file_bytes
                })

                await msg.reply(f"✅ Added `{attachment.filename}`! Total in stock: `{len(STOCK_LIST)}`")
                await send_stock_update(None)
            except asyncio.TimeoutError:
                await interaction.followup.send("⏰ Timed out waiting for file upload.")


class AddStockView(View):
    def __init__(self, author):
        super().__init__(timeout=120)
        self.author = author
        self.add_item(AddStockSelect())


# --- Clean Generator Panel View ---
class GeneratorView(View):
    def __init__(self):
        super().__init__(timeout=None)

    @button(label="Claim Item", style=discord.ButtonStyle.success, emoji="🎁", custom_id="claim_button")
    async def claim_button_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        user_id = interaction.user.id
        current_time = time.time()

        if user_id in USER_COOLDOWNS:
            cooldown_expiration = USER_COOLDOWNS[user_id]
            if current_time < cooldown_expiration:
                remaining_seconds = int(cooldown_expiration - current_time)
                minutes = remaining_seconds // 60
                seconds = remaining_seconds % 60
                
                embed = discord.Embed(
                    title="⏳ Cooldown Active",
                    description=f"You must wait **{minutes}m {seconds}s** before claiming another item.",
                    color=discord.Color.gold()
                )
                await interaction.response.send_message(embed=embed, ephemeral=True)
                return

        if not STOCK_LIST:
            embed = discord.Embed(
                title="❌ Out of Stock",
                description="There are currently no items available. Check back later!",
                color=discord.Color.red()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        item = STOCK_LIST.pop(0)

        USER_COOLDOWNS[user_id] = current_time + COOLDOWN_DURATION

        if item["type"] == "file":
            file_data = io.BytesIO(item["content"])
            discord_file = discord.File(file_data, filename=item["filename"])
            await interaction.response.send_message(
                content="🎉 **Here is your claimed item:**",
                file=discord_file,
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                content=f"🎉 **Here is your claimed item:**\n```\n{item['content']}\n```",
                ephemeral=True
            )

        await send_stock_update(interaction.channel)

    @button(label="My Cooldown", style=discord.ButtonStyle.secondary, emoji="⏱️", custom_id="cooldown_button")
    async def cooldown_button_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        user_id = interaction.user.id
        current_time = time.time()

        if user_id in USER_COOLDOWNS and current_time < USER_COOLDOWNS[user_id]:
            remaining_seconds = int(USER_COOLDOWNS[user_id] - current_time)
            minutes = remaining_seconds // 60
            seconds = remaining_seconds % 60
            
            embed = discord.Embed(
                title="⏱️ Cooldown Status",
                description=f"You are on cooldown for **{minutes}m {seconds}s**.",
                color=discord.Color.orange()
            )
        else:
            embed = discord.Embed(
                title="✅ Ready to Claim",
                description="You currently have **no cooldown**!",
                color=discord.Color.green()
            )

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @button(label="Stock", style=discord.ButtonStyle.primary, emoji="📦", custom_id="stock_button")
    async def stock_button_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(
            description=f"📦 Current items in stock: **`{len(STOCK_LIST)}`**",
            color=discord.Color.blue()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.event
async def on_ready():
    try:
        synced = await bot.tree.sync()
        print(f"Logged in as: {bot.user.name}")
        print(f"Synced {len(synced)} Slash Commands successfully!")
    except Exception as e:
        print(f"Failed to sync slash commands: {e}")


# --- HYBRID COMMANDS ---

@bot.hybrid_command(name='panel', description='Send the item generator panel.')
async def send_panel(ctx: commands.Context):
    global PANEL_MESSAGE
    embed = discord.Embed(
        title="✨ Item Generator",
        description="Click the button below to claim a reward privately.",
        color=discord.Color.blurple()
    )
    
    embed.add_field(
        name="⏰ Claim Limit",
        value="`1 claim` per **50 Minutes**",
        inline=True
    )
    
    embed.add_field(
        name="🔒 Privacy",
        value="Only you see your reward",
        inline=True
    )

    embed.set_footer(text="Automated Generator System • Powered by Floopy :)", icon_url=bot.user.display_avatar.url)
    
    if ctx.interaction:
        await ctx.send("✅ Panel deployed!", ephemeral=True)
        PANEL_MESSAGE = await ctx.channel.send(embed=embed, view=GeneratorView())
    else:
        PANEL_MESSAGE = await ctx.send(embed=embed, view=GeneratorView())

    await send_stock_update(ctx.channel)


@bot.hybrid_command(name='delpanel', description='Delete the active generator panel and stock update.')
async def del_panel(ctx: commands.Context):
    global PANEL_MESSAGE, LAST_STOCK_MESSAGE
    
    deleted_any = False
    
    if PANEL_MESSAGE:
        try:
            await PANEL_MESSAGE.delete()
            PANEL_MESSAGE = None
            deleted_any = True
        except discord.HTTPException:
            pass

    if LAST_STOCK_MESSAGE:
        try:
            await LAST_STOCK_MESSAGE.delete()
            LAST_STOCK_MESSAGE = None
            deleted_any = True
        except discord.HTTPException:
            pass

    if deleted_any:
        await ctx.send("🗑️ Panel and stock message removed!", ephemeral=True if ctx.interaction else False, delete_after=5)
    else:
        await ctx.send("⚠️ No active panel message found to delete.", ephemeral=True if ctx.interaction else False)


@bot.hybrid_command(name='addstock', description='Add new items into stock via DM.')
async def add_stock(ctx: commands.Context):
    if isinstance(ctx.channel, discord.DMChannel):
        return

    global PANEL_CHANNEL
    PANEL_CHANNEL = ctx.channel

    view = AddStockView(author=ctx.author)
    try:
        await ctx.author.send("How would you like to add stock?", view=view)
        await ctx.send("📥 Check your DMs to add stock!", ephemeral=True if ctx.interaction else False, delete_after=None if ctx.interaction else 10)
    except discord.Forbidden:
        await ctx.send("❌ Could not send DM. Please allow DMs from server members.", ephemeral=True)


@bot.hybrid_command(name='stock', description='Check how many items are currently in stock.')
async def check_stock(ctx: commands.Context):
    embed = discord.Embed(
        description=f"📦 Current items in stock: `{len(STOCK_LIST)}`",
        color=discord.Color.blue()
    )
    await ctx.send(embed=embed, ephemeral=True if ctx.interaction else False)


@bot.hybrid_command(name='bypasstimer', description='Reset cooldown timer for yourself or another member.')
async def bypass_timer(ctx: commands.Context, member: discord.Member = None):
    target = member or ctx.author

    if target.id in USER_COOLDOWNS:
        del USER_COOLDOWNS[target.id]
        if target == ctx.author:
            await ctx.send("🔓 **Cooldown reset!** You can now claim an item immediately.", ephemeral=True if ctx.interaction else False)
        else:
            await ctx.send(f"🔓 **Cooldown reset!** {target.mention} can now claim an item immediately.", ephemeral=True if ctx.interaction else False)
    else:
        if target == ctx.author:
            await ctx.send("ℹ️ You are not currently on a cooldown.", ephemeral=True if ctx.interaction else False)
        else:
            await ctx.send(f"ℹ️ {target.mention} is not currently on a cooldown.", ephemeral=True if ctx.interaction else False)


@bot.hybrid_command(name='clear', description='Clear a specified number of messages.')
async def clear_messages(ctx: commands.Context, amount: int):
    if amount <= 0:
        await ctx.send("⚠️ Please enter a number greater than 0.", ephemeral=True)
        return

    if ctx.interaction:
        await ctx.interaction.response.defer(ephemeral=True)

    deleted = await ctx.channel.purge(limit=amount if ctx.interaction else amount + 1)
    
    if ctx.interaction:
        await ctx.interaction.followup.send(f"🧹 Deleted **{len(deleted)}** messages.", ephemeral=True)
    else:
        confirm_msg = await ctx.send(f"🧹 Deleted **{len(deleted) - 1}** messages.")
        await asyncio.sleep(3)
        try:
            await confirm_msg.delete()
        except discord.HTTPException:
            pass


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CheckFailure):
        return
    elif isinstance(error, commands.MissingRequiredArgument):
        if ctx.command and ctx.command.name == 'clear':
            await ctx.send("⚠️ Please specify a number of messages! Example: `/clear amount: 12`", ephemeral=True)
    else:
        print(f"Error: {error}")


if __name__ == '__main__':
    # Start the lightweight background web server for Render's health checks
    threading.Thread(target=run_server, daemon=True).start()
    bot.run(TOKEN)
