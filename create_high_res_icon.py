from PIL import Image
import os

def create_high_res_icon():
    """Create a high-resolution ICO file with multiple sizes"""
    input_file = "trustnode_logo.png"
    
    if not os.path.exists(input_file):
        print(f"Error: {input_file} not found!")
        return
    
    # Open the original image
    img = Image.open(input_file)
    
    # Define multiple sizes for the ICO file (high quality)
    sizes = [
        (64, 64),    # Small icon
        (24, 24),    # Small icon
        (32, 32),    # Standard icon
        (48, 48),    # Large icon
        (64, 64),    # Large icon
        (128, 128),  # Extra large icon
        (256, 256),  # Extra large icon (highest quality)
        (512, 512)   # Maximum quality for modern systems
    ]
    
    # Create ICO with multiple sizes
    ico_images = []
    for size in sizes:
        # Use LANCZOS resampling for high quality
        resized_img = img.resize(size, Image.Resampling.LANCZOS)
        ico_images.append(resized_img)
    
    # Save as ICO with all sizes
    output_file = "trustnode_logo_high_res.ico"
    ico_images[0].save(
        output_file,
        format="ICO",
        sizes=[(s, s) for s in [16, 24, 32, 48, 64, 128, 256, 512]],
        append_images=ico_images[8:],  # Add all other sizes
        compress_level=6  # Good balance between size and quality
    )
    
    print(f"High-resolution icon created: {output_file}")
    print("Icon includes sizes: 16x16, 24x24, 32x32, 48x48, 64x64, 128x128, 256x256, 512x512")

if __name__ == "__main__":
    create_high_res_icon()