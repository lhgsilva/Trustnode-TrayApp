from PIL import Image
import os

def convert_to_ico():
    # Convert PNG to ICO with multiple sizes
    img = Image.open("trustnode_logo.png")
    
    # Resize to multiple sizes for the ICO file
    sizes = [(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    
    # Create a list of resized images
    icon_sizes = []
    for size in sizes:
        resized_img = img.resize(size, Image.Resampling.LANCZOS)
        icon_sizes.append(resized_img)
    
    # Save as ICO with multiple sizes
    icon_sizes[0].save("trustnode_logo.ico", format="ICO", sizes=[(s, s) for s in [16, 32, 48, 64, 128, 256]])
    
    print("Icon converted to trustnode_logo.ico")

if __name__ == "__main__":
    if os.path.exists("trustnode_logo.png"):
        convert_to_ico()
    else:
        print("trustnode_logo.png not found in current directory")