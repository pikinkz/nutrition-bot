import os
import logging
import asyncio
import sqlite3
from datetime import datetime, timedelta
import json
import base64
from typing import Dict, Any, Optional
import schedule
import time
from threading import Thread

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
import google.generativeai as genai
from PIL import Image
import io

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Configuration from environment variables
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
AUTHORIZED_USER_ID = int(os.getenv('AUTHORIZED_USER_ID', '0'))

# Configure Gemini
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-1.5-flash')

class NutritionBot:
    def __init__(self):
        self.init_database()
        self.user_data = {}
        self.pending_meals = {}

    def init_database(self):
        """Initialize SQLite database"""
        conn = sqlite3.connect('nutrition_data.db')
        cursor = conn.cursor()

        # User profile table
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

        # Meals table
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
                fiber REAL,
                image_analysis TEXT,
                FOREIGN KEY (user_id) REFERENCES user_profile (user_id)
            )
        ''')

        conn.commit()
        conn.close()

    def get_user_profile(self, user_id: int) -> Optional[Dict]:
        """Get user profile from database"""
        conn = sqlite3.connect('nutrition_data.db')
        cursor = conn.cursor()

        cursor.execute('SELECT * FROM user_profile WHERE user_id = ?', (user_id,))
        row = cursor.fetchone()
        conn.close()

        if row:
            return {
                'user_id': row[0],
                'age': row[1],
                'weight': row[2],
                'height': row[3],
                'sex': row[4],
                'activity_level': row[5],
                'protein_goal': row[6],
                'created_at': row[7]
            }
        return None

    def save_user_profile(self, user_id: int, profile: Dict):
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

    def log_meal(self, user_id: int, meal_data: Dict):
        """Log a meal to the database"""
        conn = sqlite3.connect('nutrition_data.db')
        cursor = conn.cursor()

        cursor.execute('''
            INSERT INTO meals
            (user_id, date, food_description, calories, protein, carbs, fat, fiber, image_analysis)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (user_id, datetime.now().date(), meal_data['description'],
              meal_data['calories'], meal_data['protein'], meal_data['carbs'],
              meal_data['fat'], meal_data['fiber'], meal_data['analysis']))

        conn.commit()
        conn.close()

    def get_daily_totals(self, user_id: int, date: str = None) -> Dict:
        """Get daily nutrition totals"""
        if not date:
            date = datetime.now().date()

        conn = sqlite3.connect('nutrition_data.db')
        cursor = conn.cursor()

        cursor.execute('''
            SELECT SUM(calories), SUM(protein), SUM(carbs), SUM(fat), SUM(fiber)
            FROM meals WHERE user_id = ? AND date = ?
        ''', (user_id, date))

        result = cursor.fetchone()
        conn.close()

        return {
            'calories': result[0] or 0,
            'protein': result[1] or 0,
            'carbs': result[2] or 0,
            'fat': result[3] or 0,
            'fiber': result[4] or 0
        }

    async def analyze_food_image(self, image_data: bytes) -> Dict:
        """Analyze food image using Gemini API"""
        try:
            # Convert image data to PIL Image
            image = Image.open(io.BytesIO(image_data))

            prompt = """
            Analyze this food image and provide nutritional information. Be very specific about:

            1. FOOD IDENTIFICATION: What foods do you see? Be specific about cooking methods, ingredients.

            2. PORTION SIZE ASSESSMENT:
               - Can you determine the portion size from the image?
               - Are there reference objects (utensils, hands, plates) that help with scale?
               - If portion size is unclear, what questions should be asked?

            3. NUTRITIONAL ESTIMATE: Provide estimates for:
               - Calories
               - Protein (g)
               - Carbohydrates (g)
               - Fat (g)
               - Fiber (g)

            4. CONFIDENCE LEVEL: How confident are you in this estimate?

            5. ADDITIONAL QUESTIONS: What questions would help improve accuracy?

            If this is NOT a food image, clearly state that and refuse to analyze.

            Respond in JSON format:
            {
                "is_food": true/false,
                "food_items": ["item1", "item2"],
                "portion_confidence": "high/medium/low",
                "nutrition": {
                    "calories": number,
                    "protein": number,
                    "carbs": number,
                    "fat": number,
                    "fiber": number
                },
                "questions": ["question1", "question2"],
                "motivational_comment": "encouraging comment about the food choice",
                "suggestions": "any suggestions for improvement"
            }
            """

            response = model.generate_content([prompt, image])

            # Parse JSON response
            try:
                # Clean the response text and extract JSON
                response_text = response.text.strip()
                if response_text.startswith('```json'):
                    response_text = response_text[7:-3]
                elif response_text.startswith('```'):
                    response_text = response_text[3:-3]

                return json.loads(response_text)
            except json.JSONDecodeError:
                # Fallback if JSON parsing fails
                return {
                    "is_food": False,
                    "error": "Could not parse nutritional analysis"
                }

        except Exception as e:
            logger.error(f"Error analyzing image: {e}")
            return {
                "is_food": False,
                "error": "Failed to analyze image"
            }

# Initialize bot
nutrition_bot = NutritionBot()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start command handler"""
    user_id = update.effective_user.id

    # Check if user is authorized
    if user_id != AUTHORIZED_USER_ID:
        await update.message.reply_text("Sorry, this bot is private.")
        return

    profile = nutrition_bot.get_user_profile(user_id)

    if not profile:
        await update.message.reply_text(
            "Welcome to your personal nutrition tracker! 🥗\n\n"
            "I'll help you track your meals and reach your nutrition goals.\n"
            "First, I need some information about you.\n\n"
            "What's your age?"
        )
        context.user_data['setup_step'] = 'age'
    else:
        await update.message.reply_text(
            f"Welcome back! 👋\n\n"
            f"Your current protein goal: {profile['protein_goal']}g per day\n\n"
            f"Send me a food photo to get started, or use /stats to see today's progress!"
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

    # Handle portion size clarification
    if user_id in nutrition_bot.pending_meals:
        await handle_portion_clarification(update, context, text)
        return

    # Regular message
    await update.message.reply_text(
        "Send me a photo of your food to analyze! 📸\n"
        "Or use /stats to see your daily progress."
    )

async def handle_setup(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    """Handle user profile setup"""
    user_id = update.effective_user.id
    step = context.user_data.get('setup_step')

    if 'profile_data' not in context.user_data:
        context.user_data['profile_data'] = {}

    if step == 'age':
        try:
            age = int(text)
            context.user_data['profile_data']['age'] = age
            context.user_data['setup_step'] = 'weight'
            await update.message.reply_text("Great! What's your weight in kg?")
        except ValueError:
            await update.message.reply_text("Please enter a valid age (number only)")

    elif step == 'weight':
        try:
            weight = float(text)
            context.user_data['profile_data']['weight'] = weight
            context.user_data['setup_step'] = 'height'
            await update.message.reply_text("What's your height in cm?")
        except ValueError:
            await update.message.reply_text("Please enter a valid weight (number only)")

    elif step == 'height':
        try:
            height = float(text)
            context.user_data['profile_data']['height'] = height
            context.user_data['setup_step'] = 'sex'

            keyboard = [
                [InlineKeyboardButton("Male", callback_data='sex_male')],
                [InlineKeyboardButton("Female", callback_data='sex_female')]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text("What's your sex?", reply_markup=reply_markup)
        except ValueError:
            await update.message.reply_text("Please enter a valid height (number only)")

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
            [InlineKeyboardButton("Sedentary (office job)", callback_data='activity_sedentary')],
            [InlineKeyboardButton("Lightly Active (light exercise)", callback_data='activity_light')],
            [InlineKeyboardButton("Moderately Active (regular exercise)", callback_data='activity_moderate')],
            [InlineKeyboardButton("Very Active (intense exercise)", callback_data='activity_very')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text("What's your activity level?", reply_markup=reply_markup)

    elif query.data.startswith('activity_'):
        activity = query.data.split('_')[1]
        context.user_data['profile_data']['activity_level'] = activity

        # Calculate protein goal based on weight and activity
        weight = context.user_data['profile_data']['weight']
        protein_goal = calculate_protein_goal(weight, activity)
        context.user_data['profile_data']['protein_goal'] = protein_goal

        # Save profile
        nutrition_bot.save_user_profile(user_id, context.user_data['profile_data'])

        await query.edit_message_text(
            f"Perfect! Your profile is set up! ✅\n\n"
            f"📊 Your daily protein goal: {protein_goal}g\n"
            f"(Based on {weight}kg body weight and {activity} activity level)\n\n"
            f"You can adjust this anytime with /set_protein_goal\n\n"
            f"Now send me a food photo to get started! 📸"
        )

        # Clean up setup data
        del context.user_data['setup_step']
        del context.user_data['profile_data']

    elif query.data.startswith('log_'):
        meal_id = query.data.split('_')[1]
        if user_id in nutrition_bot.pending_meals and meal_id in nutrition_bot.pending_meals[user_id]:
            meal_data = nutrition_bot.pending_meals[user_id][meal_id]
            nutrition_bot.log_meal(user_id, meal_data)

            # Get daily totals
            daily_totals = nutrition_bot.get_daily_totals(user_id)
            profile = nutrition_bot.get_user_profile(user_id)
            protein_percentage = (daily_totals['protein'] / profile['protein_goal']) * 100

            await query.edit_message_text(
                f"✅ Meal logged successfully!\n\n"
                f"📊 Today's totals:\n"
                f"• Calories: {daily_totals['calories']:.0f}\n"
                f"• **Protein: {daily_totals['protein']:.1f}g ({protein_percentage:.0f}% of goal)** 🎯\n"
                f"• Carbs: {daily_totals['carbs']:.1f}g\n"
                f"• Fat: {daily_totals['fat']:.1f}g\n"
                f"• Fiber: {daily_totals['fiber']:.1f}g"
            )

            # Clean up pending meal
            del nutrition_bot.pending_meals[user_id][meal_id]

    elif query.data == 'cancel_log':
        await query.edit_message_text("Meal not logged. Send another photo anytime! 📸")

def calculate_protein_goal(weight_kg: float, activity_level: str) -> float:
    """Calculate protein goal based on weight and activity level"""
    # Convert kg to lbs for calculation
    weight_lbs = weight_kg * 2.20462

    # Protein multipliers (grams per pound)
    multipliers = {
        'sedentary': 0.8,
        'light': 1.0,
        'moderate': 1.2,
        'very': 1.4
    }

    return round(weight_lbs * multipliers.get(activity_level, 1.0))

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle food photo uploads"""
    user_id = update.effective_user.id

    if user_id != AUTHORIZED_USER_ID:
        return

    # Check if user profile exists
    profile = nutrition_bot.get_user_profile(user_id)
    if not profile:
        await update.message.reply_text("Please set up your profile first with /start")
        return

    await update.message.reply_text("🔍 Analyzing your food photo...")

    # Get the photo
    photo = update.message.photo[-1]  # Get highest resolution
    file = await context.bot.get_file(photo.file_id)

    # Download image data
    image_data = await file.download_as_bytearray()

    # Analyze with Gemini
    analysis = await nutrition_bot.analyze_food_image(bytes(image_data))

    if not analysis.get('is_food', False):
        await update.message.reply_text(
            "I don't see any food in this image. Please send a clear photo of your meal! 🍽️"
        )
        return

    # Check if we need to ask questions about portion size
    if analysis.get('portion_confidence') == 'low' and analysis.get('questions'):
        meal_id = str(int(time.time()))

        # Store pending meal data
        if user_id not in nutrition_bot.pending_meals:
            nutrition_bot.pending_meals[user_id] = {}

        nutrition_bot.pending_meals[user_id][meal_id] = {
            'description': ', '.join(analysis['food_items']),
            'analysis': json.dumps(analysis),
            'calories': analysis['nutrition']['calories'],
            'protein': analysis['nutrition']['protein'],
            'carbs': analysis['nutrition']['carbs'],
            'fat': analysis['nutrition']['fat'],
            'fiber': analysis['nutrition']['fiber']
        }

        question_text = "I need help with portion size:\n\n" + "\n".join(analysis['questions'])
        await update.message.reply_text(question_text)

        # Store meal ID for later reference
        context.user_data['pending_meal_id'] = meal_id

    else:
        # Direct analysis, show results
        await show_nutrition_analysis(update, context, analysis, user_id)

async def show_nutrition_analysis(update: Update, context: ContextTypes.DEFAULT_TYPE, analysis: Dict, user_id: int):
    """Show nutrition analysis and ask for confirmation"""
    nutrition = analysis['nutrition']

    # Get user's protein goal
    profile = nutrition_bot.get_user_profile(user_id)
    protein_goal = profile['protein_goal']
    protein_percentage = (nutrition['protein'] / protein_goal) * 100

    # Create meal description
    food_items = ', '.join(analysis['food_items'])

    message = f"🍽️ **{food_items}**\n\n"
    message += f"📊 **Nutrition Analysis:**\n"
    message += f"• Calories: {nutrition['calories']:.0f}\n"
    message += f"• **Protein: {nutrition['protein']:.1f}g ({protein_percentage:.0f}% of daily goal)** 🎯\n"
    message += f"• Carbs: {nutrition['carbs']:.1f}g\n"
    message += f"• Fat: {nutrition['fat']:.1f}g\n"
    message += f"• Fiber: {nutrition['fiber']:.1f}g\n\n"

    if analysis.get('motivational_comment'):
        message += f"💭 {analysis['motivational_comment']}\n\n"

    if analysis.get('suggestions'):
        message += f"💡 {analysis['suggestions']}\n\n"

    meal_id = str(int(time.time()))

    # Store pending meal
    if user_id not in nutrition_bot.pending_meals:
        nutrition_bot.pending_meals[user_id] = {}

    nutrition_bot.pending_meals[user_id][meal_id] = {
        'description': food_items,
        'analysis': json.dumps(analysis),
        'calories': nutrition['calories'],
        'protein': nutrition['protein'],
        'carbs': nutrition['carbs'],
        'fat': nutrition['fat'],
        'fiber': nutrition['fiber']
    }

    # Create buttons
    keyboard = [
        [InlineKeyboardButton("✅ Log this meal", callback_data=f'log_{meal_id}')],
        [InlineKeyboardButton("❌ Cancel", callback_data='cancel_log')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(message, reply_markup=reply_markup, parse_mode='Markdown')

async def handle_portion_clarification(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    """Handle portion size clarification"""
    user_id = update.effective_user.id
    meal_id = context.user_data.get('pending_meal_id')

    if not meal_id or user_id not in nutrition_bot.pending_meals:
        return

    await update.message.reply_text("🔍 Updating portion size estimate...")

    # Get stored meal data
    meal_data = nutrition_bot.pending_meals[user_id][meal_id]
    original_analysis = json.loads(meal_data['analysis'])

    # Ask Gemini to adjust the portion based on user input
    adjustment_prompt = f"""
    The user provided this clarification about portion size: "{text}"

    Original analysis: {json.dumps(original_analysis)}

    Please adjust the nutritional values based on the user's input and return updated JSON:
    {{
        "nutrition": {{
            "calories": number,
            "protein": number,
            "carbs": number,
            "fat": number,
            "fiber": number
        }},
        "adjustment_comment": "explanation of how you adjusted the portions"
    }}
    """

    try:
        response = model.generate_content(adjustment_prompt)
        response_text = response.text.strip()

        # Clean JSON response
        if response_text.startswith('```json'):
            response_text = response_text[7:-3]
        elif response_text.startswith('```'):
            response_text = response_text[3:-3]

        adjusted_analysis = json.loads(response_text)

        # Update meal data
        nutrition = adjusted_analysis['nutrition']
        meal_data.update({
            'calories': nutrition['calories'],
            'protein': nutrition['protein'],
            'carbs': nutrition['carbs'],
            'fat': nutrition['fat'],
            'fiber': nutrition['fiber']
        })

        # Update original analysis
        original_analysis['nutrition'] = nutrition
        meal_data['analysis'] = json.dumps(original_analysis)

        # Show updated analysis
        await show_nutrition_analysis(update, context, original_analysis, user_id)

        # Add adjustment comment
        if adjusted_analysis.get('adjustment_comment'):
            await update.message.reply_text(f"📝 {adjusted_analysis['adjustment_comment']}")

    except Exception as e:
        logger.error(f"Error adjusting portion: {e}")
        await update.message.reply_text("Sorry, I couldn't process that adjustment. Please try again.")

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

    message = f"📊 **Today's Nutrition Summary**\n\n"
    message += f"• Calories: {daily_totals['calories']:.0f}\n"
    message += f"• **Protein: {daily_totals['protein']:.1f}g / {profile['protein_goal']:.0f}g ({protein_percentage:.0f}%)** 🎯\n"
    message += f"• Carbs: {daily_totals['carbs']:.1f}g\n"
    message += f"• Fat: {daily_totals['fat']:.1f}g\n"
    message += f"• Fiber: {daily_totals['fiber']:.1f}g\n\n"

    if protein_percentage >= 100:
        message += "🎉 Congratulations! You've reached your protein goal!"
    elif protein_percentage >= 75:
        message += "💪 Great progress! You're almost at your protein goal!"
    else:
        remaining = profile['protein_goal'] - daily_totals['protein']
        message += f"🎯 You need {remaining:.0f}g more protein to reach your goal!"

    await update.message.reply_text(message, parse_mode='Markdown')

def main():
    """Main function to run the bot"""
    if not TELEGRAM_TOKEN or not GEMINI_API_KEY:
        print("Please set TELEGRAM_TOKEN and GEMINI_API_KEY environment variables")
        return

    # Create application
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(CallbackQueryHandler(handle_callback))

    # Run the bot
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
