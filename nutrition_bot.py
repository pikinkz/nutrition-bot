import os
import logging
import sqlite3
import json
import time
import tempfile
from datetime import datetime, date, timedelta

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
import google.generativeai as genai
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# --- Configuration ---
# Configure logging to see bot activity and errors
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Load credentials and settings from environment variables
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
# Make sure to set your Telegram user ID in the environment variables
AUTHORIZED_USER_ID = int(os.getenv('AUTHORIZED_USER_ID', '0'))

# Configure the Gemini AI model
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-1.5-flash')

# --- Bot Class ---
class NutritionBot:
    """Handles all database interactions and AI analysis."""
    def __init__(self):
        self.init_database()
        # A dictionary to temporarily hold meal data before it's confirmed for logging
        self.pending_meals = {}

    def init_database(self):
        """Initializes the SQLite database and creates tables if they don't exist."""
        conn = sqlite3.connect('nutrition_data.db')
        cursor = conn.cursor()
        
        # Table for user profile data
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_profile (
                user_id INTEGER PRIMARY KEY,
                age INTEGER,
                weight REAL,
                height REAL,
                sex TEXT,
                activity_level TEXT,
                protein_goal REAL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Table for logged meals
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS meals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                date DATE,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                food_description TEXT,
                calories REAL,
                protein REAL,
                carbs REAL,
                fat REAL,
                FOREIGN KEY (user_id) REFERENCES user_profile (user_id)
            )
        ''')
        
        conn.commit()
        conn.close()

    def get_user_profile(self, user_id: int):
        """Gets a user's profile from the database."""
        conn = sqlite3.connect('nutrition_data.db')
        conn.row_factory = sqlite3.Row # Allows accessing columns by name
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM user_profile WHERE user_id = ?', (user_id,))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None

    def save_user_profile(self, user_id: int, profile: dict):
        """Saves or updates a user's profile in the database."""
        conn = sqlite3.connect('nutrition_data.db')
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO user_profile 
            (user_id, age, weight, height, sex, activity_level, protein_goal)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (user_id, profile.get('age'), profile.get('weight'), profile.get('height'), 
              profile.get('sex'), profile.get('activity_level'), profile.get('protein_goal')))
        conn.commit()
        conn.close()

    def log_meal(self, user_id: int, meal_data: dict):
        """Logs a meal to the database."""
        conn = sqlite3.connect('nutrition_data.db')
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO meals (user_id, date, food_description, calories, protein, carbs, fat)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (user_id, datetime.now().date(), meal_data['description'], 
              meal_data['calories'], meal_data['protein'], meal_data['carbs'], meal_data['fat']))
        conn.commit()
        conn.close()

    def get_daily_totals(self, user_id: int, target_date: date):
        """Gets nutrition totals for a specific date."""
        conn = sqlite3.connect('nutrition_data.db')
        cursor = conn.cursor()
        cursor.execute('''
            SELECT SUM(calories), SUM(protein), SUM(carbs), SUM(fat)
            FROM meals WHERE user_id = ? AND date = ?
        ''', (user_id, target_date))
        result = cursor.fetchone()
        conn.close()
        
        return {
            'calories': result[0] or 0, 'protein': result[1] or 0,
            'carbs': result[2] or 0, 'fat': result[3] or 0
        }

    def get_last_meal(self, user_id: int):
        """Gets the most recently logged meal for a user."""
        conn = sqlite3.connect('nutrition_data.db')
        cursor = conn.cursor()
        cursor.execute(
            'SELECT id, food_description FROM meals WHERE user_id = ? ORDER BY timestamp DESC LIMIT 1',
            (user_id,)
        )
        result = cursor.fetchone()
        conn.close()
        return result if result else None

    def delete_meal(self, meal_id: int):
        """Deletes a meal from the database by its ID."""
        conn = sqlite3.connect('nutrition_data.db')
        cursor = conn.cursor()
        cursor.execute('DELETE FROM meals WHERE id = ?', (meal_id,))
        conn.commit()
        conn.close()

    async def _analyze_with_gemini(self, content):
        """Generic helper to call Gemini API and parse JSON response."""
        prompt = """
        Analyze the provided food content (image or text) and return nutritional information in a clean JSON format.
        The JSON object must have this exact structure:
        {
            "is_food": true or false,
            "food_items": ["item1", "item2", ...],
            "nutrition": {
                "calories": number,
                "protein": number,
                "carbs": number,
                "fat": number
            },
            "confidence": "high/medium/low",
            "comment": "A short, motivational comment about the food."
        }
        If the content is not food, set "is_food" to false and fill other fields with null or zero.
        """
        try:
            response = model.generate_content([prompt, content])
            response_text = response.text.strip().replace('```json', '').replace('```', '')
            return json.loads(response_text)
        except Exception as e:
            logger.error(f"Error in Gemini analysis: {e}")
            return {"is_food": False, "error": "Failed to analyze content"}

    async def analyze_food_image(self, image_data: bytes):
        """Analyzes a food image using the Gemini API."""
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix='.jpg') as tmp_file:
                tmp_file.write(image_data)
                tmp_file_path = tmp_file.name
            
            try:
                uploaded_file = genai.upload_file(tmp_file_path, mime_type='image/jpeg')
                analysis = await self._analyze_with_gemini(uploaded_file)
                genai.delete_file(uploaded_file.name)
            finally:
                os.unlink(tmp_file_path) # Clean up the temporary file
            
            return analysis
        except Exception as e:
            logger.error(f"Error processing image file: {e}")
            return {"is_food": False, "error": "Failed to process image file"}

    async def analyze_food_text(self, text: str):
        """Analyzes a food description text using the Gemini API."""
        return await self._analyze_with_gemini(text)

# --- Bot Initialization ---
nutrition_bot = NutritionBot()

# --- Authorization Decorator ---
def authorized(handler):
    """Decorator to restrict access to the authorized user."""
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id
        if user_id != AUTHORIZED_USER_ID:
            # Silently ignore unauthorized users, or reply with a message
            # await update.message.reply_text("Sorry, this bot is private.")
            logger.warning(f"Unauthorized access attempt by user_id: {user_id}")
            return
        return await handler(update, context, *args, **kwargs)
    return wrapper

# --- Command Handlers ---
@authorized
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /start command, initiating profile setup if needed."""
    user_id = update.effective_user.id
    profile = nutrition_bot.get_user_profile(user_id)
    
    if not profile:
        await update.message.reply_text(
            "Welcome! I'm your personal nutrition bot. To get started, I need some info.\n\n"
            "First, what's your age?"
        )
        context.user_data['setup_step'] = 'age'
        context.user_data['profile_data'] = {}
    else:
        await update.message.reply_text(
            f"Welcome back! Your daily protein goal is {profile['protein_goal']:.0f}g.\n\n"
            "Send a food photo or text description (e.g., 'an apple and 2 eggs') to log a meal. 📸"
        )

@authorized
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Displays a list of all available commands."""
    help_text = (
        "Here are the available commands:\n\n"
        "*/start* - Initialize the bot or begin profile setup.\n\n"
        "*/stats* `[date|yesterday]` - Show nutrition summary for today, yesterday, or a specific date (e.g., `/stats 2025-07-10`).\n\n"
        "*/profile* - View your current user profile.\n\n"
        "*/editprofile* `<field> <value>` - Update your profile. Example: `/editprofile weight 85`.\n\n"
        "*/deletelast* - Remove the most recent meal you logged.\n\n"
        "*/help* - Show this help message.\n\n"
        "You can also send a photo or a text description of your meal to log it."
    )
    await update.message.reply_text(help_text, parse_mode='Markdown')


@authorized
async def profile_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /profile command, displaying user data."""
    profile = nutrition_bot.get_user_profile(update.effective_user.id)
    if not profile:
        await update.message.reply_text("No profile found. Please run /start to set one up.")
        return

    message = "👤 *Your Profile*\n\n"
    message += f"• *Age:* {profile['age']}\n"
    message += f"• *Weight:* {profile['weight']} kg\n"
    message += f"• *Height:* {profile['height']} cm\n"
    message += f"• *Sex:* {profile['sex'].capitalize()}\n"
    message += f"• *Activity:* {profile['activity_level'].capitalize()}\n"
    message += f"• *Protein Goal:* {profile['protein_goal']:.0f}g / day"
    
    await update.message.reply_text(message, parse_mode='Markdown')

@authorized
async def edit_profile_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /editprofile command to update profile fields."""
    args = context.args
    if len(args) != 2:
        await update.message.reply_text(
            "Please use the format: `/editprofile <field> <value>`\n\n"
            "*Example:* `/editprofile weight 85.5`\n\n"
            "Available fields: `age`, `weight`, `height`",
            parse_mode='Markdown'
        )
        return

    field, value = args[0].lower(), args[1]
    valid_fields = ['age', 'weight', 'height']
    if field not in valid_fields:
        await update.message.reply_text(f"Invalid field. Please choose from: {', '.join(valid_fields)}")
        return

    user_id = update.effective_user.id
    profile = nutrition_bot.get_user_profile(user_id)
    if not profile:
        await update.message.reply_text("No profile found. Please run /start first.")
        return

    try:
        # Update the profile dictionary and recalculate goals if weight changes
        if field == 'age':
            profile[field] = int(value)
        else:
            profile[field] = float(value)
        
        if field == 'weight':
            multiplier = {'sedentary': 0.8, 'active': 1.2, 'very': 1.6}
            profile['protein_goal'] = round(profile['weight'] * multiplier.get(profile['activity_level'], 1.0))

        nutrition_bot.save_user_profile(user_id, profile)
        await update.message.reply_text(
            f"✅ Profile updated!\n*{field.capitalize()}:* {value}\n"
            f"New protein goal: {profile['protein_goal']:.0f}g",
            parse_mode='Markdown'
        )
    except ValueError:
        await update.message.reply_text("Invalid value. Please provide a number for the selected field.")

@authorized
async def delete_last_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /deletelast command to remove the last meal."""
    user_id = update.effective_user.id
    last_meal = nutrition_bot.get_last_meal(user_id)

    if not last_meal:
        await update.message.reply_text("❌ No meals logged yet today to delete.")
        return
    
    meal_id, description = last_meal
    nutrition_bot.delete_meal(meal_id)
    await update.message.reply_text(f"✅ Last meal deleted: '{description}'")

@authorized
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows daily stats for today, yesterday, or a specific date."""
    user_id = update.effective_user.id
    profile = nutrition_bot.get_user_profile(user_id)
    if not profile:
        await update.message.reply_text("Please set up your profile first with /start")
        return

    target_date = date.today()
    if context.args:
        date_str = context.args[0].lower()
        if date_str == 'yesterday':
            target_date = date.today() - timedelta(days=1)
        else:
            try:
                target_date = datetime.strptime(date_str, '%Y-%m-%d').date()
            except ValueError:
                await update.message.reply_text("Invalid date format. Use YYYY-MM-DD or 'yesterday'.")
                return
    
    daily_totals = nutrition_bot.get_daily_totals(user_id, target_date)
    protein_percentage = (daily_totals['protein'] / profile['protein_goal']) * 100 if profile['protein_goal'] > 0 else 0
    
    date_formatted = "Today's" if target_date == date.today() else f"{target_date.strftime('%A, %b %d')}"
    message = f"📊 *{date_formatted} Summary*\n\n"
    message += f"• *Calories:* {daily_totals['calories']:.0f}\n"
    message += f"• *Protein:* {daily_totals['protein']:.1f}g / {profile['protein_goal']:.0f}g ({protein_percentage:.0f}%)\n"
    message += f"• *Carbs:* {daily_totals['carbs']:.1f}g\n"
    message += f"• *Fat:* {daily_totals['fat']:.1f}g\n"
    
    await update.message.reply_text(message, parse_mode='Markdown')

# --- Message and Photo Handlers ---
@authorized
async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles text messages for setup or food logging."""
    user_id = update.effective_user.id
    text = update.message.text
    
    # Handle user setup if it's in progress
    if 'setup_step' in context.user_data:
        await handle_setup(update, context, text)
        return
    
    # Otherwise, treat the text as a food description
    await update.message.reply_text("🔍 Analyzing food description...")
    analysis = await nutrition_bot.analyze_food_text(text)
    await process_analysis_result(update, context, analysis)

@authorized
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles food photo uploads."""
    profile = nutrition_bot.get_user_profile(update.effective_user.id)
    if not profile:
        await update.message.reply_text("Please set up your profile first with /start")
        return
    
    await update.message.reply_text("🔍 Analyzing food photo...")
    
    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    image_data = await file.download_as_bytearray()
    
    analysis = await nutrition_bot.analyze_food_image(bytes(image_data))
    await process_analysis_result(update, context, analysis)

async def process_analysis_result(update: Update, context: ContextTypes.DEFAULT_TYPE, analysis: dict):
    """Shared function to process and display analysis from Gemini."""
    user_id = update.effective_user.id
    profile = nutrition_bot.get_user_profile(user_id)

    if not analysis.get('is_food', False):
        await update.message.reply_text("I couldn't identify any food in that. Please try a clearer description or photo. 🍽️")
        return
    
    nutrition = analysis['nutrition']
    protein_goal = profile['protein_goal']
    protein_percentage = (nutrition['protein'] / protein_goal) * 100 if protein_goal > 0 else 0
    food_items = ', '.join(analysis['food_items'])
    
    message = f"🍽️ *{food_items.title()}*\n\n"
    message += f"📊 *Nutrition Estimate*\n"
    message += f"• *Calories:* {nutrition['calories']:.0f}\n"
    message += f"• *Protein:* {nutrition['protein']:.1f}g ({protein_percentage:.0f}% of goal)\n"
    message += f"• *Carbs:* {nutrition['carbs']:.1f}g\n"
    message += f"• *Fat:* {nutrition['fat']:.1f}g\n\n"
    
    if analysis.get('comment'):
        message += f"💭 _{analysis['comment']}_\n\n"
    
    meal_id = str(int(time.time()))
    
    # Store pending meal data
    if user_id not in nutrition_bot.pending_meals:
        nutrition_bot.pending_meals[user_id] = {}
    
    nutrition_bot.pending_meals[user_id][meal_id] = {
        'description': food_items, 'calories': nutrition['calories'],
        'protein': nutrition['protein'], 'carbs': nutrition['carbs'], 'fat': nutrition['fat']
    }
    
    keyboard = [
        [InlineKeyboardButton("✅ Log this meal", callback_data=f'log_{meal_id}')],
        [InlineKeyboardButton("❌ Cancel", callback_data='cancel')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(message, reply_markup=reply_markup, parse_mode='Markdown')

# --- Setup and Callback Handlers ---
async def handle_setup(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    """Guides the user through the initial profile setup."""
    step = context.user_data.get('setup_step')
    profile_data = context.user_data['profile_data']
    
    try:
        if step == 'age':
            profile_data['age'] = int(text)
            context.user_data['setup_step'] = 'weight'
            await update.message.reply_text("Great! What's your current weight in kg?")
        
        elif step == 'weight':
            profile_data['weight'] = float(text)
            context.user_data['setup_step'] = 'height'
            await update.message.reply_text("Got it. And your height in cm?")
        
        elif step == 'height':
            profile_data['height'] = float(text)
            context.user_data['setup_step'] = 'sex'
            keyboard = [[InlineKeyboardButton(s, callback_data=f'sex_{s.lower()}')] for s in ["Male", "Female"]]
            await update.message.reply_text("What's your sex?", reply_markup=InlineKeyboardMarkup(keyboard))

    except ValueError:
        await update.message.reply_text("Please enter a valid number.")

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles all button presses from inline keyboards."""
    query = update.callback_query
    user_id = query.from_user.id
    
    # Prevent unauthorized users from using callbacks
    if user_id != AUTHORIZED_USER_ID:
        await query.answer("Unauthorized", show_alert=True)
        return
    
    await query.answer() # Acknowledge the button press
    
    data = query.data
    
    if data.startswith('sex_'):
        context.user_data['profile_data']['sex'] = data.split('_')[1]
        context.user_data['setup_step'] = 'activity'
        keyboard = [[InlineKeyboardButton(s, callback_data=f'activity_{s.lower().replace(" ", "")}')] for s in ["Sedentary", "Active", "Very Active"]]
        await query.edit_message_text("Last question: How active are you?", reply_markup=InlineKeyboardMarkup(keyboard))
    
    elif data.startswith('activity_'):
        profile_data = context.user_data['profile_data']
        profile_data['activity_level'] = data.split('_')[1]
        
        # Calculate initial protein goal
        weight = profile_data['weight']
        multiplier = {'sedentary': 0.8, 'active': 1.2, 'veryactive': 1.6}
        protein_goal = round(weight * multiplier.get(profile_data['activity_level'], 1.0))
        profile_data['protein_goal'] = protein_goal
        
        nutrition_bot.save_user_profile(user_id, profile_data)
        
        await query.edit_message_text(
            f"✅ *Setup complete!*\n\n"
            f"Your daily protein goal is set to *{protein_goal:.0f}g*.\n\n"
            f"You can now send me a food photo or text description to get started!",
            parse_mode='Markdown'
        )
        
        del context.user_data['setup_step']
        del context.user_data['profile_data']
    
    elif data.startswith('log_'):
        meal_id = data.split('_')[1]
        if user_id in nutrition_bot.pending_meals and meal_id in nutrition_bot.pending_meals[user_id]:
            meal_data = nutrition_bot.pending_meals[user_id][meal_id]
            nutrition_bot.log_meal(user_id, meal_data)
            
            # Show updated daily totals after logging
            profile = nutrition_bot.get_user_profile(user_id)
            daily_totals = nutrition_bot.get_daily_totals(user_id, date.today())
            protein_percentage = (daily_totals['protein'] / profile['protein_goal']) * 100 if profile['protein_goal'] > 0 else 0
            
            await query.edit_message_text(
                f"✅ *Logged!*\n\n"
                f"Today's Summary:\n"
                f"• Cals: {daily_totals['calories']:.0f}\n"
                f"• Protein: {daily_totals['protein']:.1f}g ({protein_percentage:.0f}%)\n"
                f"• Carbs: {daily_totals['carbs']:.1f}g\n"
                f"• Fat: {daily_totals['fat']:.1f}g",
                parse_mode='Markdown'
            )
            
            del nutrition_bot.pending_meals[user_id][meal_id]

    elif data == 'cancel':
        await query.edit_message_text("❌ Action canceled.")

# --- Scheduled Tasks ---
async def send_daily_report(context: ContextTypes.DEFAULT_TYPE):
    """Scheduled job to send the previous day's nutrition summary."""
    logger.info("Running scheduled daily report job.")
    user_id = AUTHORIZED_USER_ID
    profile = nutrition_bot.get_user_profile(user_id)
    if not profile:
        logger.warning("Scheduled report skipped: No user profile found.")
        return
    
    yesterday = date.today() - timedelta(days=1)
    totals = nutrition_bot.get_daily_totals(user_id, yesterday)
    
    # Only send a report if there's data
    if totals['calories'] > 0:
        protein_percentage = (totals['protein'] / profile['protein_goal']) * 100 if profile['protein_goal'] > 0 else 0
        message = f"☀️ *Good Morning! Here's your summary for {yesterday.strftime('%A')}*\n\n"
        message += f"• *Calories:* {totals['calories']:.0f}\n"
        message += f"• *Protein:* {totals['protein']:.1f}g / {profile['protein_goal']:.0f}g ({protein_percentage:.0f}%)\n"
        message += f"• *Carbs:* {totals['carbs']:.1f}g\n"
        message += f"• *Fat:* {totals['fat']:.1f}g"
        
        await context.bot.send_message(chat_id=user_id, text=message, parse_mode='Markdown')
    else:
        logger.info("Scheduled report skipped: No meals logged yesterday.")

# --- Main Application ---
def main():
    """Main function to set up and run the bot."""
    if not all([TELEGRAM_TOKEN, GEMINI_API_KEY, AUTHORIZED_USER_ID]):
        logger.critical("CRITICAL: Missing one or more environment variables! (TELEGRAM_TOKEN, GEMINI_API_KEY, AUTHORIZED_USER_ID)")
        return
    
    logger.info("Starting bot...")
    
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    
    # Register command handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("profile", profile_command))
    application.add_handler(CommandHandler("editprofile", edit_profile_command))
    application.add_handler(CommandHandler("deletelast", delete_last_command))
    
    # Register message handlers
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))
    
    # Register callback handler for inline buttons
    application.add_handler(CallbackQueryHandler(handle_callback))
    
    # Set up and start the scheduler for daily reports
    scheduler = AsyncIOScheduler(timezone="Asia/Almaty") # Set your timezone
    scheduler.add_job(send_daily_report, 'cron', hour=8, minute=0, args=[application])
    scheduler.start()
    logger.info("Scheduler started. Daily report set for 8:00 AM.")
    
    # Start the bot
    application.run_polling()

if __name__ == '__main__':
    main()
