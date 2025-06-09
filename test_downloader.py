"""
Test script for video downloader functionality
"""

import os
import tempfile
from video_downloader import VideoDownloader


def test_video_download():
    """Test downloading a real video"""
    # Create a temporary directory for testing
    test_dir = tempfile.mkdtemp()
    downloader = VideoDownloader(download_dir=test_dir)

    # Test with a short YouTube video (Rick Roll - it's always available)
    # Alternatively, you can test with a Twitter/X.com video by uncommenting the line below
    test_url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    # test_url = "https://twitter.com/username/status/1234567890"  # Replace with a real Twitter video URL

    print(f"Testing download from: {test_url}")
    print(f"Download directory: {test_dir}")

    # Test URL detection
    platform = downloader.detect_platform(test_url)
    print(f"Detected platform: {platform}")

    # Test download (this will actually download the video)
    print("Starting download...")
    result = downloader.download_video(test_url)

    if result:
        print("Download successful!")
        print(f"Title: {result['title']}")
        print(f"Platform: {result['platform']}")
        print(f"File path: {result['filepath']}")
        print(f"File size: {result['filesize']} bytes")
        print(f"Duration: {result['duration']} seconds")

        # Check if file exists
        if os.path.exists(result['filepath']):
            print("✅ File exists on disk")

            # Clean up
            downloader.cleanup_file(result['filepath'])
            print("🗑️ File cleaned up")
        else:
            print("❌ File not found on disk")
    else:
        print("❌ Download failed")

    # Clean up test directory
    try:
        os.rmdir(test_dir)
    except:
        pass


if __name__ == "__main__":
    test_video_download()
