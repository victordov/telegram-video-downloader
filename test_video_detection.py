"""
Test script for video detection and screenshot functionality
"""

import os
import tempfile
from video_downloader import VideoDownloader


def test_video_detection():
    """Test detecting videos in URLs"""
    # Create a temporary directory for testing
    test_dir = tempfile.mkdtemp()
    downloader = VideoDownloader(download_dir=test_dir)

    # Test with a YouTube video (should contain a video)
    youtube_url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    
    # Test with a Threads post URL (may or may not contain a video)
    # Replace with a real Threads URL if needed
    threads_url = "https://www.threads.net/@meta/post/CxYWfOMPRXP"

    print("Testing video detection...")
    
    # Test YouTube URL (should contain a video)
    print(f"\nTesting URL: {youtube_url}")
    has_video = downloader.check_for_video(youtube_url)
    print(f"Contains video: {has_video}")
    
    # Test Threads URL
    print(f"\nTesting URL: {threads_url}")
    has_video = downloader.check_for_video(threads_url)
    print(f"Contains video: {has_video}")
    
    # Test screenshot functionality with Threads URL
    print("\nTesting screenshot functionality...")
    result = downloader.take_screenshot(threads_url)
    
    if result:
        print("Screenshot successful!")
        print(f"Title: {result['title']}")
        print(f"Platform: {result['platform']}")
        print(f"File path: {result['filepath']}")
        print(f"File size: {result['filesize']} bytes")
        
        # Check if file exists
        if os.path.exists(result['filepath']):
            print("✅ File exists on disk")
            
            # Clean up
            downloader.cleanup_file(result['filepath'])
            print("🗑️ File cleaned up")
        else:
            print("❌ File not found on disk")
    else:
        print("❌ Screenshot failed")
    
    # Clean up test directory
    try:
        os.rmdir(test_dir)
    except:
        pass


if __name__ == "__main__":
    test_video_detection()