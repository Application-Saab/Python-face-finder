#!/usr/bin/env python3
"""
Simple Face Recognition Script
Search for a face from sample.png in all images in the album folder.
"""

import face_recognition
import os
import cv2
import numpy as np
from PIL import Image
import matplotlib.pyplot as plt
from pathlib import Path
import time
from typing import List, Dict

class SimpleFaceSearcher:
    def __init__(self, tolerance: float = 0.45, min_confidence: float = 55.0):
        """
        Initialize the face searcher.
        
        Args:
            tolerance: Face matching tolerance (lower = more strict)
                      0.3 = very strict, 0.45 = recommended, 0.6 = lenient
            min_confidence: Minimum confidence percentage to consider a match
                           70.0 = recommended, 60.0 = lenient, 80.0 = strict
        """
        self.tolerance = tolerance
        self.min_confidence = min_confidence
        self.reference_encoding = None
        self.reference_path = None
        
        print(f"🎯 Search configured:")
        print(f"   - Tolerance: {tolerance} (lower = stricter)")
        print(f"   - Minimum confidence: {min_confidence}%")
        
    def load_reference_face(self, image_path: str) -> bool:
        """
        Load the reference face from sample.png
        
        Args:
            image_path: Path to sample.png
            
        Returns:
            bool: True if face found and loaded successfully
        """
        if not os.path.exists(image_path):
            print(f"❌ Reference image not found: {image_path}")
            return False
            
        print(f"📸 Loading reference image: {image_path}")
        
        try:
            # Load the image
            reference_image = face_recognition.load_image_file(image_path)
            
            # Find faces in the reference image
            face_encodings = face_recognition.face_encodings(reference_image)
            
            if len(face_encodings) == 0:
                print("❌ No faces found in the reference image!")
                return False
            
            if len(face_encodings) > 1:
                print(f"⚠️  Found {len(face_encodings)} faces in reference image. Using the first one.")
            
            # Store the first face encoding
            self.reference_encoding = face_encodings[0]
            self.reference_path = image_path
            
            print("✅ Reference face loaded successfully!")
            return True
            
        except Exception as e:
            print(f"❌ Error loading reference image: {e}")
            return False
    
    def search_in_album(self, album_folder: str) -> List[Dict]:
        """
        Search for the reference face in all JPG images in the album folder.
        
        Args:
            album_folder: Path to the album folder containing JPG images
            
        Returns:
            List of dictionaries containing match information
        """
        if self.reference_encoding is None:
            print("❌ No reference face loaded! Call load_reference_face() first.")
            return []
        
        if not os.path.exists(album_folder):
            print(f"❌ Album folder not found: {album_folder}")
            return []
        
        # Get all JPG files in the album folder
        jpg_files = []
        for filename in os.listdir(album_folder):
            if filename.lower().endswith('.jpg'):
                jpg_files.append(os.path.join(album_folder, filename))
        
        if not jpg_files:
            print(f"❌ No JPG files found in {album_folder}")
            return []
        
        print(f"🔍 Searching for matches in {len(jpg_files)} images...")
        print("=" * 50)
        
        matches = []
        processed_count = 0
        
        for image_path in jpg_files:
            filename = os.path.basename(image_path)
            print(f"Processing: {filename}", end=" ... ")
            
            try:
                # Load the image
                image = face_recognition.load_image_file(image_path)
                
                # Find all faces in the image
                face_locations = face_recognition.face_locations(image)
                face_encodings = face_recognition.face_encodings(image, face_locations)
                
                # Check each face against our reference
                image_matches = []
                for i, face_encoding in enumerate(face_encodings):
                    # Calculate distance (similarity)
                    face_distance = face_recognition.face_distance([self.reference_encoding], face_encoding)[0]
                    confidence = (1 - face_distance) * 100  # Convert to percentage
                    
                    # Apply both tolerance and confidence filters
                    if face_distance <= self.tolerance and confidence >= self.min_confidence:
                        match_info = {
                            'image_path': image_path,
                            'filename': filename,
                            'face_location': face_locations[i],
                            'confidence': confidence,
                            'face_distance': face_distance,
                            'face_number': i + 1
                        }
                        
                        matches.append(match_info)
                        image_matches.append(match_info)
                    elif face_distance <= self.tolerance:
                        # This is for debugging - show low confidence matches
                        print(f"[Low confidence: {confidence:.1f}%]", end=" ")
                
                # Print result for this image
                if image_matches:
                    best_match = max(image_matches, key=lambda x: x['confidence'])
                    print(f"✅ MATCH! (Confidence: {best_match['confidence']:.1f}%)")
                else:
                    print("❌ No match")
                
                processed_count += 1
                
            except Exception as e:
                print(f"❌ Error: {e}")
                continue
        
        print("=" * 50)
        print(f"🎯 Search completed!")
        print(f"📊 Processed: {processed_count} images")
        print(f"🎉 Found: {len(matches)} matches (min confidence: {self.min_confidence}%)")
        
        return matches
    
    def display_matches(self, matches: List[Dict], max_display: int = 6):
        """
        Display the matching images with bounding boxes around faces.
        
        Args:
            matches: List of match dictionaries from search_in_album()
            max_display: Maximum number of images to display
        """
        if not matches:
            print("❌ No matches to display!")
            return
        
        # Sort matches by confidence (highest first)
        sorted_matches = sorted(matches, key=lambda x: x['confidence'], reverse=True)
        display_matches = sorted_matches[:max_display]
        
        print(f"\n📸 Displaying top {len(display_matches)} matches:")
        
        # Calculate grid layout
        num_images = len(display_matches)
        cols = min(3, num_images)
        rows = (num_images + cols - 1) // cols
        
        plt.figure(figsize=(15, 5 * rows))
        
        for i, match in enumerate(display_matches):
            # Load and display the image
            image = cv2.imread(match['image_path'])
            image_rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
            
            # Draw rectangle around the matching face
            top, right, bottom, left = match['face_location']
            cv2.rectangle(image_rgb, (left, top), (right, bottom), (0, 255, 0), 3)
            
            # Add confidence text
            confidence_text = f"{match['confidence']:.1f}%"
            cv2.putText(image_rgb, confidence_text, (left, top - 10), 
                       cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 0), 2)
            
            # Display in subplot
            plt.subplot(rows, cols, i + 1)
            plt.imshow(image_rgb)
            plt.title(f"Match {i+1}: {match['filename']}\nConfidence: {match['confidence']:.1f}%")
            plt.axis('off')
        
        plt.tight_layout()
        plt.show()
    
    def save_results(self, matches: List[Dict], output_file: str = 'search_results.txt'):
        """
        Save the search results to a text file.
        
        Args:
            matches: List of match dictionaries
            output_file: Output file path
        """
        if not matches:
            print("❌ No matches to save!")
            return
        
        # Sort by confidence
        sorted_matches = sorted(matches, key=lambda x: x['confidence'], reverse=True)
        
        with open(output_file, 'w') as f:
            f.write("Face Recognition Search Results\n")
            f.write("=" * 40 + "\n")
            f.write(f"Reference Image: {self.reference_path}\n")
            f.write(f"Search Tolerance: {self.tolerance}\n")
            f.write(f"Minimum Confidence: {self.min_confidence}%\n")
            f.write(f"Total Matches Found: {len(matches)}\n\n")
            
            for i, match in enumerate(sorted_matches, 1):
                f.write(f"Match {i}:\n")
                f.write(f"  File: {match['filename']}\n")
                f.write(f"  Full Path: {match['image_path']}\n")
                f.write(f"  Confidence: {match['confidence']:.2f}%\n")
                f.write(f"  Face Distance: {match['face_distance']:.4f}\n")
                f.write(f"  Face Location: {match['face_location']}\n")
                f.write(f"  Face Number: {match['face_number']}\n")
                f.write("-" * 30 + "\n")
        
        print(f"💾 Results saved to: {output_file}")


def main():
    """
    Main function - Ready to use!
    Just make sure you have:
    1. sample.png in the same directory as this script
    2. album/ folder with JPG images
    """
    
    print("🎭 Face Recognition Search")
    print("=" * 30)
    
    # Initialize the searcher
    # Adjust tolerance if needed:
    # 0.4 = very strict (fewer false positives)
    # 0.6 = balanced (default)
    # 0.8 = lenient (more matches, possible false positives)
    searcher = SimpleFaceSearcher(tolerance=0.6)
    
    # Load the reference face from sample.png
    reference_image = "sample.png"
    if not searcher.load_reference_face(reference_image):
        print("❌ Failed to load reference image. Please check that sample.png exists and contains a face.")
        return
    
    # Search in the album folder
    album_folder = "album_resized"
    matches = searcher.search_in_album(album_folder)
    
    if matches:
        print(f"\n🎉 Found {len(matches)} matching images!")
        
        # Display the matches (comment this out if you don't want to show images)
        searcher.display_matches(matches, max_display=6)
        
        # Save results to file
        searcher.save_results(matches)
        
        # Print summary
        print("\n📋 Summary of matches:")
        sorted_matches = sorted(matches, key=lambda x: x['confidence'], reverse=True)
        for i, match in enumerate(sorted_matches[:10], 1):  # Show top 10
            print(f"  {i}. {match['filename']} - {match['confidence']:.1f}% confidence")
        
        if len(matches) > 10:
            print(f"  ... and {len(matches) - 10} more matches")
    
    else:
        print("\n😞 No matches found.")
        print("💡 Try adjusting the tolerance:")
        print("   - Lower tolerance (0.4-0.5) for stricter matching")
        print("   - Higher tolerance (0.7-0.8) for more lenient matching")


if __name__ == "__main__":
    main()