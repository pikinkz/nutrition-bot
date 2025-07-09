# Personal Nutrition Tracker Bot

A Telegram bot that analyzes food photos and tracks nutrition using Google's Gemini API.

## Features

- 📸 **Photo Analysis**: Upload food photos for instant nutritional analysis
- 🎯 **Protein Focus**: Tracks protein intake with personalized daily goals
- 🤖 **Smart Portion Detection**: Asks clarifying questions when portion size is unclear
- 📊 **Daily Tracking**: Logs meals and provides daily nutrition summaries
- 💪 **Motivational**: Provides encouragement and nutritional insights
- 🔄 **Multiple Photos**: Can analyze additional photos of the same meal for better accuracy

## Setup Instructions

### 1. Get API Keys

#### Telegram Bot Token:
1. Message `@BotFather` on Telegram
2. Send `/newbot`
3. Follow the instructions to create your bot
4. Save the token (format: `123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11`)

#### Your Telegram User ID:
1. Message `@userinfobot` on Telegram
2. Save the user ID number it shows

#### Gemini API Key:
1. Go to [Google AI Studio](https://makersuite.google.com/app/apikey)
2. Create a new API key
3. Save the key

### 2. Deploy to Render

1. Fork this repository or upload the files to GitHub
2. Go to [Render.com](https://render.com) and sign in
3. Click "New" → "Web Service"
4. Connect your GitHub repository
5. Configure the service:
   - **Name**: `nutrition-bot`
   - **Runtime**: `Python 3`
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `python nutrition_bot.py`

### 3. Set Environment Variables

In your Render service settings, add these environment variables:
- `TELEGRAM_TOKEN`: Your bot token from BotFather
- `GEMINI_API_KEY`: Your Gemini API key
- `AUTHORIZED_USER_ID`: Your Telegram user ID

### 4. Deploy

Click "Deploy" and wait for the service to start!

## Usage

1. Start the bot with `/start`
2. Complete the setup (age, weight, height, sex, activity level)
3. Send food photos to analyze nutrition
4. Use `/stats` to see daily progress

## Commands

- `/start` - Initial setup or restart
- `/stats` - View daily nutrition summary

## Database

The bot uses SQLite to store:
- User profiles (age, weight, height, sex, activity level, protein goal)
- Meal logs (date, nutrition data, descriptions)

Data is stored locally in `nutrition_data.db` on the Render service.

## Privacy

- Single-user bot (only authorized user can use it)
- Data stored locally on your Render instance
- No data sharing with third parties
