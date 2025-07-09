import os
import logging
import sqlite3
import json
import time
from datetime import datetime
import requests
from io import BytesIO
from PIL import Image

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
import google.generativeai as genai

# Configure logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Configuration
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
AUTHORIZED_USER_ID = int(os.getenv('AUTHORIZED_USER_ID', '0'))

# Configure Gemini
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-1.5-flash')

class NutritionBot:
    def __init__(self):
        self.init_database()
        self.pending_meals = {}
        
    def init_database(self):
        """Initialize SQLite database"""
        conn = sqlite3.connect('nutrition_data.db')
        cursor = conn.cursor()
        
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
        """Get user profile from database"""
        conn = sqlite3.connect('nutrition_data.db')
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM user_profile WHERE user_id = ?', (user_id,))
        row = cursor.fetchone()
        conn.close()
        
        if row:
            return {
                'user_id': row[0], 'age': row[1], 'weight': row[2], 'height': row[3], 
                'sex': row[4], 'activity_level': row[5], 'protein_goal': row[6]
            }
        return None
    
    def save_user_profile(self, user_id: int, profile: dict):
        """Save user profile to database"""
        conn = sqlite3.connect('nutrition_data.db')
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO user_profile 
            (user_id, age, weight, height, sex, activity_level, protein_goal)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (user_id, profile['age'], profile['weight'], profile['height'], 
              profile['sex'], profile['activity_level'], profile['protein_goal']))
        conn.commit()
        conn.close()
    
    def log_meal(self, user_id: int, meal_data: dict):
        """Log a meal to the database"""
        conn = sqlite3.connect('nutrition_data.db')
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO meals 
            (user_id, date, food_description, calories, protein, carbs, fat)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (user_id, datetime.now().date(), meal_data['description'], 
              meal_data['calories'], meal_data['protein'], meal_data['carbs'], meal_data['fat']))
        conn.commit()
        conn.close()
    
    def get_daily_totals(self, user_id: int):
        """Get daily nutrition totals"""
        conn = sqlite3.connect('nutrition_data.db')
        cursor = conn.cursor()
        cursor.execute('''
            SELECT SUM(calories), SUM(protein), SUM(carbs), SUM(fat)
            FROM meals WHERE user_id = ? AND date = ?
        ''', (user_id, datetime.now().date()))
        result = cursor.fetchone()
        conn.close()
        
        return {
            'calories': result[0] or 0,
            'protein': result[1] or 0,
            'carbs': result[2] or 0,
            'fat': result[3] or 0
        }

    async def analyze_food_image(self, image_url: str):
        """Analyze food image using Gemini API"""
        try:
            # Download image
            response = requests.get(image_url)
            image_data = response.content
            
            prompt = """
            Analyze this food image and provide nutritional information in JSON format:
            
            {
                "is_food": true/false,
                "food_items": ["item1", "item2"],
                "nutrition": {
                    "calories": number,
                    "protein": number,
                    "carbs": number,
                    "fat": number
                },
                "confidence": "high/medium/low",
                "comment": "motivational comment about the food"
            }
            
            If this is NOT food, set is_food to false.
            """
            
            # Create a proper image object for Gemini
            image = Image.open(BytesIO(image_data))
            
            response = model.generate_content([prompt, image])
            
            # Parse JSON response
            response_text = response.text.strip()
            if response_text.startswith('```json'):
                response_text = response_text[7:-3]
            elif response_text.startswith('```'):
                response_text = response_text[3:-3]
            
            return json.loads(response_text)
        except Exception as e:
            logger.error(f"Error analyzing image: {e}")
            return {"is_food": False, "error": "Failed to analyze image"}

# Initialize bot
nutrition_bot = NutritionBot()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start command handler"""
    user_id = update.effective_user.id
    
    if user_id != AUTHORIZED_USER_ID:
        await update.message.reply_text("Sorry, this bot is private.")
        return
    
    profile = nutrition_bot.get_user_profile(user_id)
    
    if not profile:
        await update.message.reply_text(
            "Welcome! I'll help you track nutrition. First, what's your age?"
        )
        context.user_data['setup_step'] = 'age'
        context.user_data['profile_data'] = {}
    else:
        await update.message.reply_text(
            f"Welcome back! Your protein goal: {profile['protein_goal']}g\n"
            f"Send a food photo to analyze! 📸"
        )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle text messages"""
    user_id = update.effective_user.id
    
    if user_id != AUTHORIZED_USER_ID:
        return
    
    text = update.message.text
    
    # Handle user setup
    if 'setup_step' in context.user_data:
        await handle_setup(update, context, text)
        return
    
    await update.message.reply_text("Send me a food photo to analyze! 📸")

async def handle_setup(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    """Handle user profile setup"""
    user_id = update.effective_user.id
    step = context.user_data.get('setup_step')
    
    if step == 'age':
        try:
            context.user_data['profile_data']['age'] = int(text)
            context.user_data['setup_step'] = 'weight'
            await update.message.reply_text("Great! What's your weight in kg?")
        except ValueError:
            await update.message.reply_text("Please enter a valid age")
    
    elif step == 'weight':
        try:
            weight = float(text)
            context.user_data['profile_data']['weight'] = weight
            context.user_data['setup_step'] = 'height'
            await update.message.reply_text("What's your height in cm?")
        except ValueError:
            await update.message.reply_text("Please enter a valid weight")
    
    elif step == 'height':
        try:
            context.user_data['profile_data']['height'] = float(text)
            context.user_data['setup_step'] = 'sex'
            
            keyboard = [
                [InlineKeyboardButton("Male", callback_data='sex_male')],
                [InlineKeyboardButton("Female", callback_data='sex_female')]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text("What's your sex?", reply_markup=reply_markup)
        except ValueError:
            await update.message.reply_text("Please enter a valid height")

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle button callbacks"""
    query = update.callback_query
    user_id = query.from_user.id
    
    if user_id != AUTHORIZED_USER_ID:
        return
    
    await query.answer()
    
    if query.data.startswith('sex_'):
        sex = query.data.split('_')[1]
        context.user_data['profile_data']['sex'] = sex
        context.user_data['setup_step'] = 'activity'
        
        keyboard = [
            [InlineKeyboardButton("Sedentary", callback_data='activity_sedentary')],
            [InlineKeyboardButton("Active", callback_data='activity_active')],
            [InlineKeyboardButton("Very Active", callback_data='activity_very')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text("Activity level?", reply_markup=reply_markup)
    
    elif query.data.startswith('activity_'):
        activity = query.data.split('_')[1]
        context.user_data['profile_data']['activity_level'] = activity
        
        # Calculate protein goal
        weight = context.user_data['profile_data']['weight']
        multiplier = {'sedentary': 0.8, 'active': 1.2, 'very': 1.6}
        protein_goal = round(weight * multiplier.get(activity, 1.0))
        context.user_data['profile_data']['protein_goal'] = protein_goal
        
        # Save profile
        nutrition_bot.save_user_profile(user_id, context.user_data['profile_data'])
        
        await query.edit_message_text(
            f"✅ Setup complete!\n"
            f"Daily protein goal: {protein_goal}g\n\n"
            f"Send me a food photo! 📸"
        )
        
        del context.user_data['setup_step']
        del context.user_data['profile_data']
    
    elif query.data.startswith('log_'):
        meal_id = query.data.split('_')[1]
        if user_id in nutrition_bot.pending_meals and meal_id in nutrition_bot.pending_meals[user_id]:
            meal_data = nutrition_bot.pending_meals[user_id][meal_id]
            nutrition_bot.log_meal(user_id, meal_data)
            
            daily_totals = nutrition_bot.get_daily_totals(user_id)
            profile = nutrition_bot.get_user_profile(user_id)
            protein_percentage = (daily_totals['protein'] / profile['protein_goal']) * 100
            
            await query.edit_message_text(
                f"✅ Logged!\n\n"
                f"Today's totals:\n"
                f"• Calories: {daily_totals['calories']:.0f}\n"
                f"• Protein: {daily_totals['protein']:.1f}g ({protein_percentage:.0f}%)\n"
                f"• Carbs: {daily_totals['carbs']:.1f}g\n"
                f"• Fat: {daily_totals['fat']:.1f}g"
            )
            
            del nutrition_bot.pending_meals[user_id][meal_id]

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle food photo uploads"""
    user_id = update.effective_user.id
    
    if user_id != AUTHORIZED_USER_ID:
        return
    
    profile = nutrition_bot.get_user_profile(user_id)
    if not profile:
        await update.message.reply_text("Please set up your profile first with /start")
        return
    
    await update.message.reply_text("🔍 Analyzing food...")
    
    # Get photo URL
    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    file_url = file.file_path
    
    # Analyze with Gemini
    analysis = await nutrition_bot.analyze_food_image(file_url)
    
    if not analysis.get('is_food', False):
        await update.message.reply_text("I don't see food in this image. Please send a clear food photo! 🍽️")
        return
    
    # Show results
    nutrition = analysis['nutrition']
    protein_goal = profile['protein_goal']
    protein_percentage = (nutrition['protein'] / protein_goal) * 100
    
    food_items = ', '.join(analysis['food_items'])
    
    message = f"🍽️ {food_items}\n\n"
    message += f"📊 Nutrition:\n"
    message += f"• Calories: {nutrition['calories']:.0f}\n"
    message += f"• Protein: {nutrition['protein']:.1f}g ({protein_percentage:.0f}%)\n"
    message += f"• Carbs: {nutrition['carbs']:.1f}g\n"
    message += f"• Fat: {nutrition['fat']:.1f}g\n\n"
    
    if analysis.get('comment'):
        message += f"💭 {analysis['comment']}\n\n"
    
    meal_id = str(int(time.time()))
    
    # Store pending meal
    if user_id not in nutrition_bot.pending_meals:
        nutrition_bot.pending_meals[user_id] = {}
    
    nutrition_bot.pending_meals[user_id][meal_id] = {
        'description': food_items,
        'calories': nutrition['calories'],
        'protein': nutrition['protein'],
        'carbs': nutrition['carbs'],
        'fat': nutrition['fat']
    }
    
    keyboard = [
        [InlineKeyboardButton("✅ Log this meal", callback_data=f'log_{meal_id}')],
        [InlineKeyboardButton("❌ Cancel", callback_data='cancel')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(message, reply_markup=reply_markup)

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show daily stats"""
    user_id = update.effective_user.id
    
    if user_id != AUTHORIZED_USER_ID:
        return
    
    profile = nutrition_bot.get_user_profile(user_id)
    if not profile:
        await update.message.reply_text("Please set up your profile first with /start")
        return
    
    daily_totals = nutrition_bot.get_daily_totals(user_id)
    protein_percentage = (daily_totals['protein'] / profile['protein_goal']) * 100
    
    message = f"📊 Today's Summary\n\n"
    message += f"• Calories: {daily_totals['calories']:.0f}\n"
    message += f"• Protein: {daily_totals['protein']:.1f}g / {profile['protein_goal']:.0f}g ({protein_percentage:.0f}%)\n"
    message += f"• Carbs: {daily_totals['carbs']:.1f}g\n"
    message += f"• Fat: {daily_totals['fat']:.1f}g\n"
    
    await update.message.reply_text(message)

def main():
    """Main function"""
    if not all([TELEGRAM_TOKEN, GEMINI_API_KEY, AUTHORIZED_USER_ID]):
        print("Missing environment variables!")
        return
    
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(CallbackQueryHandler(handle_callback))
    
    application.run_polling()

if __name__ == '__main__':
    main()
