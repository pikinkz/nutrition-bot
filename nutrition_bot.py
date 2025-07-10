import os
import logging
import sqlite3
import json
import time as python_time
import tempfile
from datetime import datetime, date, timedelta, time as dt_time

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
import google.generativeai as genai

# --- Configuration ---
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
AUTHORIZED_USER_ID = int(os.getenv('AUTHORIZED_USER_ID', '0'))

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-1.5-flash')

# --- Bot Class ---
class NutritionBot:
    """Handles all database interactions and AI analysis."""
    def __init__(self):
        self.init_database()
        self.pending_meals = {}

    def init_database(self):
        conn = sqlite3.connect('nutrition_data.db')
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_profile (
                user_id INTEGER PRIMARY KEY, age INTEGER, weight REAL, height REAL,
                sex TEXT, activity_level TEXT, protein_goal REAL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS meals (
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, date DATE,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP, food_description TEXT,
                calories REAL, protein REAL, carbs REAL, fat REAL,
                FOREIGN KEY (user_id) REFERENCES user_profile (user_id)
            )
        ''')
        conn.commit()
        conn.close()

    def get_user_profile(self, user_id: int):
        conn = sqlite3.connect('nutrition_data.db')
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM user_profile WHERE user_id = ?', (user_id,))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None

    def save_user_profile(self, user_id: int, profile: dict):
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
        conn = sqlite3.connect('nutrition_data.db')
        cursor = conn.cursor()
        cursor.execute('SELECT SUM(calories), SUM(protein), SUM(carbs), SUM(fat) FROM meals WHERE user_id = ? AND date = ?', (user_id, target_date))
        result = cursor.fetchone()
        conn.close()
        return {'calories': result[0] or 0, 'protein': result[1] or 0, 'carbs': result[2] or 0, 'fat': result[3] or 0}

    def get_last_meal(self, user_id: int):
        conn = sqlite3.connect('nutrition_data.db')
        cursor = conn.cursor()
        cursor.execute('SELECT id, food_description FROM meals WHERE user_id = ? ORDER BY timestamp DESC LIMIT 1', (user_id,))
        result = cursor.fetchone()
        conn.close()
        return result

    def delete_meal(self, meal_id: int):
        conn = sqlite3.connect('nutrition_data.db')
        cursor = conn.cursor()
        cursor.execute('DELETE FROM meals WHERE id = ?', (meal_id,))
        conn.commit()
        conn.close()

    async def _analyze_with_gemini(self, prompt_parts: list):
        """Generic helper to call Gemini API and parse JSON response."""
        try:
            response = await model.generate_content_async(prompt_parts)
            response_text = response.text.strip().replace('```json', '').replace('```', '')
            return json.loads(response_text)
        except Exception as e:
            logger.error(f"Error in Gemini analysis: {e}")
            return {"is_food": False, "error": "Failed to analyze content"}

    async def analyze_initial_content(self, content):
        """Performs the first analysis of a food photo or text."""
        prompt = """
        Analyze the provided food content (image or text) and return nutritional information in a clean JSON format.
        The JSON object must have this exact structure:
        {
            "is_food": true or false,
            "food_items": ["item1", "item2", ...],
            "nutrition": { "calories": number, "protein": number, "carbs": number, "fat": number },
            "confidence": "high/medium/low",
            "comment": "A short, motivational comment about the food."
        }
        If the content is not food, set "is_food" to false and fill other fields with null or zero.
        """
        return await self._analyze_with_gemini([prompt, content])

    async def analyze_refined_content(self, original_analysis: dict, correction_content):
        """Performs a re-analysis based on user correction."""
        prompt = f"""
        A user wants to correct a food analysis.
        Original Analysis: {json.dumps(original_analysis)}
        User's Correction: The user has provided the following new information (text or a new image).
        
        Please provide a new, updated nutritional analysis in the same JSON format as the original, taking the user's correction into account.
        For example, if they say 'the portion was bigger', increase the nutritional values. If they say 'I had 2 scoops', multiply the values.
        """
        return await self._analyze_with_gemini([prompt, correction_content])

# --- Bot Initialization ---
nutrition_bot = NutritionBot()

# --- Authorization Decorator ---
def authorized(handler):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        if update.effective_user.id != AUTHORIZED_USER_ID:
            logger.warning(f"Unauthorized access by user_id: {update.effective_user.id}")
            return
        return await handler(update, context, *args, **kwargs)
    return wrapper

# --- Command Handlers ---
@authorized
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    profile = nutrition_bot.get_user_profile(update.effective_user.id)
    if not profile:
        await update.message.reply_text("Welcome! To get started, what's your age?")
        context.user_data['setup_step'] = 'age'
        context.user_data['profile_data'] = {}
    else:
        await update.message.reply_text(f"Welcome back! Your protein goal: {profile['protein_goal']:.0f}g. Send a photo or text to log a meal.")

@authorized
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "*/start* - Initialize the bot.\n"
        "*/stats* `[date|yesterday]` - Show summary for a specific date (e.g., `/stats 2025-07-10`).\n"
        "*/profile* - View your user profile.\n"
        "*/editprofile* `<field> <value>` - Update profile. Ex: `/editprofile weight 85`.\n"
        "*/deletelast* - Remove the last meal logged.\n"
        "*/help* - Show this message."
    )
    await update.message.reply_text(help_text, parse_mode='Markdown')

@authorized
async def profile_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    args = context.args
    if len(args) != 2:
        await update.message.reply_text("Use: `/editprofile <field> <value>`\nFields: `age`, `weight`, `height`", parse_mode='Markdown')
        return
    field, value = args[0].lower(), args[1]
    if field not in ['age', 'weight', 'height']:
        await update.message.reply_text("Invalid field.")
        return
    user_id = update.effective_user.id
    profile = nutrition_bot.get_user_profile(user_id)
    if not profile:
        await update.message.reply_text("No profile found. Run /start.")
        return
    try:
        profile[field] = float(value)
        if field == 'weight':
            multiplier = {'sedentary': 0.8, 'active': 1.2, 'very': 1.6}
            profile['protein_goal'] = round(profile['weight'] * multiplier.get(profile['activity_level'], 1.0))
        nutrition_bot.save_user_profile(user_id, profile)
        await update.message.reply_text(f"✅ Profile updated! New protein goal: {profile['protein_goal']:.0f}g")
    except ValueError:
        await update.message.reply_text("Invalid value.")

@authorized
async def delete_last_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    last_meal = nutrition_bot.get_last_meal(user_id)
    if not last_meal:
        await update.message.reply_text("❌ No meals to delete.")
        return
    meal_id, description = last_meal
    nutrition_bot.delete_meal(meal_id)
    await update.message.reply_text(f"✅ Last meal deleted: '{description}'")

@authorized
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
async def handle_generic_message(update: Update, context: ContextTypes.DEFAULT_TYPE, content):
    """Handles all incoming content (text/photo) for analysis or refinement."""
    if 'setup_step' in context.user_data:
        await handle_setup(update, context, update.message.text)
        return
    
    if 'refining_meal_id' in context.user_data:
        await update.message.reply_text("🔍 Re-analyzing with your corrections...")
        meal_id = context.user_data['refining_meal_id']
        original_analysis = nutrition_bot.pending_meals[update.effective_user.id][meal_id]['analysis']
        
        new_analysis = await nutrition_bot.analyze_refined_content(original_analysis, content)
        
        del context.user_data['refining_meal_id']
        
        await process_analysis_result(update, context, new_analysis, is_refined=True)
    else:
        await update.message.reply_text("🔍 Analyzing food...")
        analysis = await nutrition_bot.analyze_initial_content(content)
        await process_analysis_result(update, context, analysis)

@authorized
async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await handle_generic_message(update, context, update.message.text)

@authorized
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    image_data = await file.download_as_bytearray()

    with tempfile.NamedTemporaryFile(delete=False, suffix='.jpg') as tmp_file:
        tmp_file.write(image_data)
        uploaded_file = await genai.upload_file_async(tmp_file.name, mime_type='image/jpeg')
    
    await handle_generic_message(update, context, uploaded_file)


async def process_analysis_result(update: Update, context: ContextTypes.DEFAULT_TYPE, analysis: dict, is_refined: bool = False):
    """Shared function to process and display analysis from Gemini."""
    user_id = update.effective_user.id
    profile = nutrition_bot.get_user_profile(user_id)

    if not analysis.get('is_food', False):
        await update.message.reply_text("I couldn't identify any food. Please try again.")
        return
    
    nutrition = analysis['nutrition']
    protein_goal = profile['protein_goal']
    protein_percentage = (nutrition['protein'] / protein_goal) * 100 if protein_goal > 0 else 0
    food_items = ', '.join(analysis['food_items'])
    
    message = f"🍽️ *{food_items.title()}*\n\n"
    if is_refined:
        message = "✅ *Refined Analysis*\n\n" + message
    message += f"📊 *Nutrition Estimate*\n"
    message += f"• Cals: {nutrition['calories']:.0f}, Protein: {nutrition['protein']:.1f}g ({protein_percentage:.0f}%)\n"
    message += f"• Carbs: {nutrition['carbs']:.1f}g, Fat: {nutrition['fat']:.1f}g\n\n"
    if analysis.get('comment'):
        message += f"💭 _{analysis['comment']}_\n\n"
    
    meal_id = str(int(python_time.time()))
    
    if user_id not in nutrition_bot.pending_meals:
        nutrition_bot.pending_meals[user_id] = {}
    
    nutrition_bot.pending_meals[user_id][meal_id] = {
        'description': food_items, 'calories': nutrition['calories'],
        'protein': nutrition['protein'], 'carbs': nutrition['carbs'], 'fat': nutrition['fat'],
        'analysis': analysis
    }
    
    keyboard = [
        [InlineKeyboardButton("✅ Log Meal", callback_data=f'log_{meal_id}')],
        [InlineKeyboardButton("✍️ Refine / Correct", callback_data=f'refine_{meal_id}')],
        [InlineKeyboardButton("❌ Cancel", callback_data='cancel')]
    ]
    await update.message.reply_text(message, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

# --- Setup and Callback Handlers ---
async def handle_setup(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    step = context.user_data.get('setup_step')
    profile_data = context.user_data['profile_data']
    try:
        if step == 'age':
            profile_data['age'] = int(text)
            context.user_data['setup_step'] = 'weight'
            await update.message.reply_text("Great! What's your weight in kg?")
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
    query = update.callback_query
    user_id = query.from_user.id
    if user_id != AUTHORIZED_USER_ID:
        await query.answer("Unauthorized", show_alert=True)
        return
    
    await query.answer()
    data = query.data
    
    if data.startswith('sex_') or data.startswith('activity_'):
        await complete_setup(query, context, data)
    elif data.startswith('log_'):
        meal_id = data.split('_')[1]
        if user_id in nutrition_bot.pending_meals and meal_id in nutrition_bot.pending_meals[user_id]:
            meal_data = nutrition_bot.pending_meals[user_id][meal_id]
            nutrition_bot.log_meal(user_id, meal_data)
            if meal_id in nutrition_bot.pending_meals[user_id]:
                 del nutrition_bot.pending_meals[user_id][meal_id]
            await query.edit_message_text("✅ Meal logged successfully!")
            await stats_command(update, context) 
    elif data.startswith('refine_'):
        meal_id = data.split('_')[1]
        context.user_data['refining_meal_id'] = meal_id
        await query.edit_message_text("Okay, what needs to be corrected? Send me a text (e.g., 'the portion was bigger') or a new photo.")
    elif data == 'cancel':
        if 'refining_meal_id' in context.user_data:
            del context.user_data['refining_meal_id']
        await query.edit_message_text("❌ Action canceled.")

async def complete_setup(query: Update, context: ContextTypes.DEFAULT_TYPE, data: str):
    profile_data = context.user_data['profile_data']
    if data.startswith('sex_'):
        profile_data['sex'] = data.split('_')[1]
        context.user_data['setup_step'] = 'activity'
        keyboard = [[InlineKeyboardButton(s, callback_data=f'activity_{s.lower().replace(" ", "")}')] for s in ["Sedentary", "Active", "Very Active"]]
        await query.edit_message_text("Last question: How active are you?", reply_markup=InlineKeyboardMarkup(keyboard))
    elif data.startswith('activity_'):
        profile_data['activity_level'] = data.split('_')[1]
        weight = profile_data['weight']
        multiplier = {'sedentary': 0.8, 'active': 1.2, 'veryactive': 1.6}
        protein_goal = round(weight * multiplier.get(profile_data['activity_level'], 1.0))
        profile_data['protein_goal'] = protein_goal
        nutrition_bot.save_user_profile(query.from_user.id, profile_data)
        await query.edit_message_text(f"✅ Setup complete! Protein goal: {protein_goal:.0f}g. Send a photo/text to start!", parse_mode='Markdown')
        del context.user_data['setup_step']
        del context.user_data['profile_data']

# --- Scheduled Tasks ---
async def send_daily_report(context: ContextTypes.DEFAULT_TYPE):
    logger.info("Running scheduled daily report job.")
    user_id = AUTHORIZED_USER_ID
    profile = nutrition_bot.get_user_profile(user_id)
    if not profile:
        logger.warning("Scheduled report skipped: No user profile.")
        return
    yesterday = date.today() - timedelta(days=1)
    totals = nutrition_bot.get_daily_totals(user_id, yesterday)
    if totals['calories'] > 0:
        protein_percentage = (totals['protein'] / profile['protein_goal']) * 100 if profile['protein_goal'] > 0 else 0
        message = f"☀️ *Good Morning! Summary for {yesterday.strftime('%A')}*\n\n"
        message += f"• Cals: {totals['calories']:.0f}, Protein: {totals['protein']:.1f}g ({protein_percentage:.0f}%)\n"
        message += f"• Carbs: {totals['carbs']:.1f}g, Fat: {totals['fat']:.1f}g"
        await context.bot.send_message(chat_id=user_id, text=message, parse_mode='Markdown')
    else:
        logger.info("Scheduled report skipped: No meals logged yesterday.")

# --- Main Application ---
def main() -> None:
    """Start the bot."""
    if not all([TELEGRAM_TOKEN, GEMINI_API_KEY, AUTHORIZED_USER_ID]):
        logger.critical("CRITICAL: Missing environment variables!")
        return
    logger.info("Starting bot...")
    
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    
    # Register handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("profile", profile_command))
    application.add_handler(CommandHandler("editprofile", edit_profile_command))
    application.add_handler(CommandHandler("deletelast", delete_last_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(CallbackQueryHandler(handle_callback))
    
    # Schedule daily report
    job_queue = application.job_queue
    report_time = dt_time(hour=3, minute=0) # 3 AM UTC for 8 AM GMT+5
    job_queue.run_daily(send_daily_report, time=report_time, chat_id=AUTHORIZED_USER_ID)
    logger.info(f"Scheduler started. Daily report set for {report_time} UTC.")
    
    # Run the bot until the user presses Ctrl-C
    application.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()
