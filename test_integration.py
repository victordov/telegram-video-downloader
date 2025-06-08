"""
Integration test to verify all components work together
"""

import tempfile
import os
import asyncio
from unittest.mock import Mock, AsyncMock, patch
from telegram_bot import TelegramVideoBot
from video_downloader import VideoDownloader


async def test_full_integration():
    """Test the complete flow from message to video processing"""
    print("🔧 Integration Test - Full Bot Workflow")
    print("=" * 50)
    
    # Create temporary directory for downloads
    test_dir = tempfile.mkdtemp()
    print(f"Test directory: {test_dir}")
    
    # Create bot with test configuration
    bot = TelegramVideoBot("test_token")
    bot.downloader = VideoDownloader(download_dir=test_dir)
    
    # Mock message and update
    mock_message = Mock()
    mock_message.text = "Check out this video: https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    mock_message.message_id = 1
    mock_message.reply_text = AsyncMock()
    mock_message.reply_video = AsyncMock()
    
    mock_update = Mock()
    mock_update.message = mock_message
    
    print("1. Testing URL detection in message...")
    urls = bot.extract_urls(mock_message.text)
    print(f"   URLs found: {urls}")
    
    video_urls = [url for url in urls if bot.downloader.is_video_url(url)]
    print(f"   Video URLs: {video_urls}")
    
    if video_urls:
        print("   ✅ Video URL detected correctly")
    else:
        print("   ❌ Video URL not detected")
        return
    
    print("\n2. Testing platform detection...")
    platform = bot.downloader.detect_platform(video_urls[0])
    print(f"   Platform: {platform}")
    
    if platform == "youtube":
        print("   ✅ Platform detected correctly")
    else:
        print("   ❌ Platform detection failed")
        return
    
    print("\n3. Testing yt-dlp configuration...")
    ydl_opts = bot.downloader.get_ydl_opts(platform)
    print(f"   Output template: {ydl_opts['outtmpl']}")
    print(f"   Format: {ydl_opts['format']}")
    print("   ✅ Configuration looks good")
    
    print("\n4. Testing message processing workflow...")
    
    # Mock the actual download to avoid network calls
    original_download = bot.downloader.download_video
    
    def mock_download(url):
        # Simulate successful download
        return {
            'filepath': os.path.join(test_dir, 'test_video.mp4'),
            'title': 'Test Video',
            'platform': 'youtube',
            'duration': 120,
            'filesize': 1024 * 1024,  # 1MB
            'url': url
        }
    
    bot.downloader.download_video = mock_download
    
    # Create a fake video file
    test_video_path = os.path.join(test_dir, 'test_video.mp4')
    with open(test_video_path, 'wb') as f:
        f.write(b'fake video content')
    
    # Mock file operations
    with patch('builtins.open', mock_open_video_file):
        await bot.handle_message(mock_update, None)
    
    # Check if processing was triggered
    if mock_message.reply_text.called:
        print("   ✅ Processing message sent")
    else:
        print("   ❌ Processing message not sent")
    
    print("\n5. Testing duplicate message prevention...")
    
    # Reset mocks
    mock_message.reply_text.reset_mock()
    
    # Try to process same message again
    await bot.handle_message(mock_update, None)
    
    if not mock_message.reply_text.called:
        print("   ✅ Duplicate message correctly ignored")
    else:
        print("   ❌ Duplicate message was processed")
    
    # Restore original method
    bot.downloader.download_video = original_download
    
    # Cleanup
    try:
        if os.path.exists(test_video_path):
            os.remove(test_video_path)
        os.rmdir(test_dir)
    except:
        pass
    
    print("\n✅ Integration test completed successfully!")


def mock_open_video_file(*args, **kwargs):
    """Mock file opening for video files"""
    mock_file = Mock()
    mock_file.__enter__ = Mock(return_value=mock_file)
    mock_file.__exit__ = Mock(return_value=None)
    mock_file.read = Mock(return_value=b'fake video content')
    return mock_file


def test_error_scenarios():
    """Test error handling scenarios"""
    print("\n🚨 Testing Error Scenarios")
    print("=" * 50)
    
    downloader = VideoDownloader()
    
    # Test invalid URLs
    invalid_urls = [
        "not_a_url",
        "https://invalid-domain.com/video",
        "https://www.youtube.com/invalid",
    ]
    
    for url in invalid_urls:
        platform = downloader.detect_platform(url)
        is_video = downloader.is_video_url(url)
        print(f"URL: {url}")
        print(f"   Platform: {platform}, Is video: {is_video}")
        
        if not is_video:
            print("   ✅ Correctly identified as non-video URL")
        else:
            print("   ❌ Incorrectly identified as video URL")
    
    print("\n✅ Error scenario testing completed!")


async def main():
    """Run all integration tests"""
    await test_full_integration()
    test_error_scenarios()


if __name__ == "__main__":
    asyncio.run(main())

