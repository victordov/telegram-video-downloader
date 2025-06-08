"""
Simple test with a different video platform
"""

from video_downloader import VideoDownloader
import tempfile
import os


def test_simple_download():
    """Test with a simpler approach"""
    test_dir = tempfile.mkdtemp()
    downloader = VideoDownloader(download_dir=test_dir)
    
    # Test URL detection with various platforms
    test_urls = [
        "https://www.youtube.com/watch?v=jNQXAC9IVRw",  # Different YouTube video
        "https://youtu.be/jNQXAC9IVRw",  # Short URL
    ]
    
    for url in test_urls:
        print(f"\nTesting URL: {url}")
        platform = downloader.detect_platform(url)
        print(f"Platform detected: {platform}")
        
        if platform:
            print("✅ URL pattern recognition working")
        else:
            print("❌ URL pattern not recognized")
    
    print(f"\nTest directory: {test_dir}")
    
    # Clean up
    try:
        os.rmdir(test_dir)
    except:
        pass


if __name__ == "__main__":
    test_simple_download()

