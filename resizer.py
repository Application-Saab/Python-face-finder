#!/usr/bin/env python3
"""
Image Resizer for Face Recognition Preprocessing
Standardizes images to ~500KB for faster processing while maintaining face recognition quality.
"""

import os
import sys
from PIL import Image, ImageOps
import math
from pathlib import Path
from typing import Tuple, Optional
import time

class ImageResizer:
    def __init__(self, target_size_kb: int = 500, max_dimension: int = 1920, quality: int = 85):
        """
        Initialize the image resizer.
        
        Args:
            target_size_kb: Target file size in KB (default: 500KB)
            max_dimension: Maximum width or height in pixels (default: 1920px)
            quality: JPEG quality for compression (default: 85, range: 1-100)
        """
        self.target_size_kb = target_size_kb
        self.max_dimension = max_dimension
        self.quality = quality
        self.target_size_bytes = target_size_kb * 1024
        
        print(f"🎯 Resizer Configuration:")
        print(f"   - Target size: {target_size_kb}KB")
        print(f"   - Max dimension: {max_dimension}px")
        print(f"   - JPEG quality: {quality}")
    
    def get_file_size(self, file_path: str) -> int:
        """Get file size in bytes."""
        return os.path.getsize(file_path)
    
    def format_size(self, size_bytes: int) -> str:
        """Format size in human-readable format."""
        if size_bytes < 1024:
            return f"{size_bytes}B"
        elif size_bytes < 1024 * 1024:
            return f"{size_bytes / 1024:.1f}KB"
        else:
            return f"{size_bytes / (1024 * 1024):.1f}MB"
    
    def calculate_optimal_dimensions(self, width: int, height: int) -> Tuple[int, int]:
        """
        Calculate optimal dimensions while maintaining aspect ratio.
        
        Args:
            width: Original width
            height: Original height
            
        Returns:
            Tuple of (new_width, new_height)
        """
        # If image is already small enough, don't upscale
        if max(width, height) <= self.max_dimension:
            return width, height
        
        # Calculate scaling factor to fit within max_dimension
        if width > height:
            scale_factor = self.max_dimension / width
        else:
            scale_factor = self.max_dimension / height
        
        new_width = int(width * scale_factor)
        new_height = int(height * scale_factor)
        
        return new_width, new_height
    
    def resize_image(self, input_path: str, output_path: str) -> dict:
        """
        Resize a single image to target specifications.
        
        Args:
            input_path: Path to input image
            output_path: Path to save resized image
            
        Returns:
            Dictionary with processing results
        """
        try:
            # Get original file info
            original_size = self.get_file_size(input_path)
            
            # Open and process image
            with Image.open(input_path) as img:
                # Get original dimensions
                original_width, original_height = img.size
                
                # Convert to RGB if necessary (handles RGBA, grayscale, etc.)
                if img.mode != 'RGB':
                    img = img.convert('RGB')
                
                # Auto-rotate based on EXIF data
                img = ImageOps.exif_transpose(img)
                
                # Calculate optimal dimensions
                new_width, new_height = self.calculate_optimal_dimensions(
                    img.width, img.height
                )
                
                # Resize image if needed
                if (new_width, new_height) != (img.width, img.height):
                    img = img.resize((new_width, new_height), Image.Resampling.LANCZOS)
                
                # Start with the configured quality
                current_quality = self.quality
                
                # Save with initial quality
                img.save(output_path, 'JPEG', quality=current_quality, optimize=True)
                
                # If file is still too large, reduce quality iteratively
                attempts = 0
                max_attempts = 10
                
                while (self.get_file_size(output_path) > self.target_size_bytes and 
                       current_quality > 30 and attempts < max_attempts):
                    
                    current_quality -= 5
                    img.save(output_path, 'JPEG', quality=current_quality, optimize=True)
                    attempts += 1
                
                # Get final file info
                final_size = self.get_file_size(output_path)
                
                # Calculate compression ratio
                compression_ratio = ((original_size - final_size) / original_size) * 100
                
                return {
                    'success': True,
                    'original_size': original_size,
                    'final_size': final_size,
                    'original_dimensions': (original_width, original_height),
                    'final_dimensions': (new_width, new_height),
                    'compression_ratio': compression_ratio,
                    'final_quality': current_quality,
                    'attempts': attempts + 1
                }
                
        except Exception as e:
            return {
                'success': False,
                'error': str(e),
                'original_size': self.get_file_size(input_path) if os.path.exists(input_path) else 0
            }
    
    def process_folder(self, input_folder: str, output_folder: str, 
                      extensions: tuple = ('.jpg', '.jpeg', '.png', '.bmp', '.tiff')) -> dict:
        """
        Process all images in a folder.
        
        Args:
            input_folder: Source folder path
            output_folder: Destination folder path
            extensions: Supported image extensions
            
        Returns:
            Dictionary with processing statistics
        """
        
        if not os.path.exists(input_folder):
            print(f"❌ Input folder not found: {input_folder}")
            return {'success': False, 'error': 'Input folder not found'}
        
        # Create output folder
        os.makedirs(output_folder, exist_ok=True)
        
        # Find all image files
        image_files = []
        for filename in os.listdir(input_folder):
            if filename.lower().endswith(extensions):
                image_files.append(filename)
        
        if not image_files:
            print(f"❌ No image files found in {input_folder}")
            return {'success': False, 'error': 'No image files found'}
        
        print(f"📁 Processing {len(image_files)} images from {input_folder}")
        print(f"📁 Output folder: {output_folder}")
        print("=" * 60)
        
        # Process each image
        total_original_size = 0
        total_final_size = 0
        successful_count = 0
        failed_count = 0
        processing_times = []
        
        start_time = time.time()
        
        for i, filename in enumerate(image_files, 1):
            input_path = os.path.join(input_folder, filename)
            
            # Convert to JPG extension for output (standardization)
            output_filename = os.path.splitext(filename)[0] + '.jpg'
            output_path = os.path.join(output_folder, output_filename)
            
            print(f"[{i:3d}/{len(image_files)}] {filename}", end=" ... ")
            
            # Time the processing
            file_start_time = time.time()
            result = self.resize_image(input_path, output_path)
            file_time = time.time() - file_start_time
            processing_times.append(file_time)
            
            if result['success']:
                total_original_size += result['original_size']
                total_final_size += result['final_size']
                successful_count += 1
                
                # Show results
                original_size_str = self.format_size(result['original_size'])
                final_size_str = self.format_size(result['final_size'])
                
                print(f"✅ {original_size_str} → {final_size_str} "
                      f"({result['compression_ratio']:.1f}% smaller) "
                      f"[{file_time:.1f}s]")
                
                # Show dimension changes if significant
                orig_w, orig_h = result['original_dimensions']
                final_w, final_h = result['final_dimensions']
                if (orig_w, orig_h) != (final_w, final_h):
                    print(f"      📐 {orig_w}×{orig_h} → {final_w}×{final_h}")
                
            else:
                failed_count += 1
                print(f"❌ Error: {result['error']}")
        
        # Calculate statistics
        total_time = time.time() - start_time
        avg_time_per_image = sum(processing_times) / len(processing_times) if processing_times else 0
        total_space_saved = total_original_size - total_final_size
        space_saved_percentage = (total_space_saved / total_original_size * 100) if total_original_size > 0 else 0
        
        print("=" * 60)
        print("📊 Processing Complete!")
        print(f"✅ Successfully processed: {successful_count} images")
        print(f"❌ Failed: {failed_count} images")
        print(f"⏱️  Total time: {total_time:.1f} seconds")
        print(f"⚡ Average time per image: {avg_time_per_image:.1f} seconds")
        print(f"💾 Total space saved: {self.format_size(total_space_saved)} ({space_saved_percentage:.1f}%)")
        print(f"📁 Original total size: {self.format_size(total_original_size)}")
        print(f"📁 Final total size: {self.format_size(total_final_size)}")
        
        return {
            'success': True,
            'total_files': len(image_files),
            'successful_count': successful_count,
            'failed_count': failed_count,
            'total_time': total_time,
            'avg_time_per_image': avg_time_per_image,
            'total_original_size': total_original_size,
            'total_final_size': total_final_size,
            'space_saved': total_space_saved,
            'space_saved_percentage': space_saved_percentage
        }
    
    def resize_single_image(self, input_path: str, output_path: str = None) -> dict:
        """
        Resize a single image (convenience method).
        
        Args:
            input_path: Path to input image
            output_path: Path to save resized image (optional)
            
        Returns:
            Processing result dictionary
        """
        if output_path is None:
            # Create output path with '_resized' suffix
            path_obj = Path(input_path)
            output_path = str(path_obj.parent / f"{path_obj.stem}_resized.jpg")
        
        print(f"📸 Resizing single image: {input_path}")
        
        result = self.resize_image(input_path, output_path)
        
        if result['success']:
            print(f"✅ Success! {self.format_size(result['original_size'])} → "
                  f"{self.format_size(result['final_size'])} "
                  f"({result['compression_ratio']:.1f}% reduction)")
        else:
            print(f"❌ Error: {result['error']}")
        
        return result


def main():
    """
    Main function - Ready to use!
    Run: python resizer.py
    """
    
    print("🖼️  Image Resizer for Face Recognition")
    print("=" * 40)
    
    # Configuration - adjust these if needed
    TARGET_SIZE_KB = 500        # Target file size
    MAX_DIMENSION = 1920        # Max width/height in pixels  
    JPEG_QUALITY = 85          # Initial JPEG quality (1-100)
    
    # Folder paths
    INPUT_FOLDER = "album"
    OUTPUT_FOLDER = "album_resized"
    
    # Initialize resizer
    resizer = ImageResizer(
        target_size_kb=TARGET_SIZE_KB,
        max_dimension=MAX_DIMENSION,
        quality=JPEG_QUALITY
    )
    
    # Check if input folder exists
    if not os.path.exists(INPUT_FOLDER):
        print(f"❌ Input folder '{INPUT_FOLDER}' not found!")
        print("💡 Make sure your images are in the 'album' folder.")
        return
    
    # Process all images
    result = resizer.process_folder(INPUT_FOLDER, OUTPUT_FOLDER)
    
    if result['success'] and result['successful_count'] > 0:
        print(f"\n🎉 All done! Your resized images are in '{OUTPUT_FOLDER}'")
        print("💡 Now you can run face recognition on the resized images for faster processing!")
        print(f"💡 Update your face_search.py to use '{OUTPUT_FOLDER}' instead of '{INPUT_FOLDER}'")
    else:
        print(f"\n😞 Processing completed with issues.")


if __name__ == "__main__":
    main()