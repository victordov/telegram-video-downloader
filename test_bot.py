"""
Mock test for Telegram bot functionality
Tests message handling and URL detection without requiring a real bot token
"""

import asyncio
import tempfile
import os
from unittest.mock import Mock, AsyncMock
from telegram_bot import TelegramVideoBot


class MockMessage:
    """Mock Telegram message for testing"""
    def __init__(self, text: str, message_id: int = 1):
        self.text = text
        self.message_id = message_id
        self.reply_text = AsyncMock()
        self.reply_video = AsyncMock()
        self.delete = AsyncMock()


class MockUpdate:
    """Mock Telegram update for testing"""
    def __init__(self, message: MockMessage):
        self.message = message


async def test_bot_functionality():
    """Test bot message handling functionality"""
    print("🧪 Testing Telegram Bot Functionality")
    print("=" * 50)
    
    # Create bot instance with dummy token
    bot = TelegramVideoBot("dummy_token")
    
    # Test URL extraction
    test_messages = [
        "Check out this video: https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "Multiple URLs: https://youtu.be/abc123 and https://www.instagram.com/p/example/",
        "No URLs in this message",
        "Mixed content with https://www.tiktok.com/@user/video/123 and some text",
        "Non-video URL: https://www.google.com"
    ]
    
    print("1. Testing URL extraction:")
    for i, text in enumerate(test_messages, 1):
        urls = bot.extract_urls(text)
        print(f"   Message {i}: {text[:50]}...")
        print(f"   URLs found: {urls}")
        
        # Check which URLs are video URLs
        video_urls = [url for url in urls if bot.downloader.is_video_url(url)]
        print(f"   Video URLs: {video_urls}")
        print()
    
    print("2. Testing message processing logic:")
    
    # Test message with video URL
    video_message = MockMessage("Check this out: https://www.youtube.com/watch?v=dQw4w9WgXcQ", 1)
    video_update = MockUpdate(video_message)
    
    print("   Processing message with YouTube URL...")
    
    # Mock the process_video_url method to avoid actual download
    original_process = bot.process_video_url
    bot.process_video_url = AsyncMock()
    
    await bot.handle_message(video_update, None)
    
    # Check if process_video_url was called
    if bot.process_video_url.called:
        print("   ✅ Video URL processing triggered")
        print(f"   ✅ Called with URL: {bot.process_video_url.call_args[0][1]}")
    else:
        print("   ❌ Video URL processing not triggered")
    
    # Restore original method
    bot.process_video_url = original_process
    
    print("\n3. Testing duplicate message handling:")
    
    # Test duplicate message (same ID)
    bot.process_video_url = AsyncMock()
    await bot.handle_message(video_update, None)  # Same message again
    
    if not bot.process_video_url.called:
        print("   ✅ Duplicate message correctly ignored")
    else:
        print("   ❌ Duplicate message was processed")
    
    print("\n4. Testing command handlers:")
    
    # Test start command
    start_message = MockMessage("/start", 2)
    start_update = MockUpdate(start_message)
    
    await bot.start_command(start_update, None)
    
    if start_message.reply_text.called:
        print("   ✅ Start command responded")
    else:
        print("   ❌ Start command did not respond")
    
    # Test help command
    help_message = MockMessage("/help", 3)
    help_update = MockUpdate(help_message)
    
    await bot.help_command(help_update, None)
    
    if help_message.reply_text.called:
        print("   ✅ Help command responded")
    else:
        print("   ❌ Help command did not respond")
    
    print("\n✅ All tests completed!")


def test_url_patterns():
    """Test URL pattern matching for all platforms"""
    print("\n🔍 Testing URL Pattern Matching")
    print("=" * 50)
    
    bot = TelegramVideoBot("dummy_token")
    
    test_cases = [
        # YouTube
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "youtube"),
        ("https://youtu.be/dQw4w9WgXcQ", "youtube"),
        ("https://www.youtube.com/shorts/abc123", "youtube"),
        
        # Instagram
        ("https://www.instagram.com/p/ABC123/", "instagram"),
        ("https://www.instagram.com/reel/XYZ789/", "instagram"),
        ("https://instagram.com/tv/DEF456/", "instagram"),
        
        # TikTok
        ("https://www.tiktok.com/@user/video/123456789", "tiktok"),
        ("https://vm.tiktok.com/ZMeABC123/", "tiktok"),
        ("https://tiktok.com/t/ZTdABC123/", "tiktok"),
        
        # Facebook
        ("https://www.facebook.com/user/videos/123456789", "facebook"),
        ("https://www.facebook.com/watch/?v=123456789", "facebook"),
        ("https://fb.watch/abc123def/", "facebook"),
        
        # Non-video URLs
        ("https://www.google.com", None),
        ("https://www.twitter.com/user/status/123", None),
        ("https://example.com/video.mp4", None),
    ]
    
    for url, expected_platform in test_cases:
        detected_platform = bot.downloader.detect_platform(url)
        is_video = bot.downloader.is_video_url(url)
        
        status = "✅" if detected_platform == expected_platform else "❌"
        print(f"{status} {url}")
        print(f"   Expected: {expected_platform}, Got: {detected_platform}, Is video: {is_video}")
        print()


async def main():
    """Run all tests"""
    await test_bot_functionality()
    test_url_patterns()


if __name__ == "__main__":
    asyncio.run(main())

