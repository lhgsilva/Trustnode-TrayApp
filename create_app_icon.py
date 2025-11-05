from PIL import Image
import os

def create_app_icon():
    """Create application icon from existing logo or default"""
    # Try to find logo in current directory
    logo_filenames = [
        "trustnode_logo.png",
        "Trustnode_logo.png",
        "trustnode_logo.PNG",
        "logo.png"
    ]
    
    # Check if any logo file exists
    logo_path = None
    for filename in logo_filenames:
        if os.path.exists(filename):
            logo_path = filename
            break
    
    if logo_path:
        try:
            # Open existing logo
            img = Image.open(logo_path)
            print(f"Using existing logo: {logo_path}")
        except Exception as e:
            print(f"Could not open logo {logo_path}: {e}")
            # Create default image
            img = Image.new('RGB', (64, 64), color='darkblue')
            from PIL import ImageDraw
            dc = ImageDraw.Draw(img)
            dc.rectangle([16, 16, 48, 48], fill='white')
            try:
                dc.text((27, 22), "T", fill='black')
            except:
                pass
    else:
        print("No logo found, creating default image")
        # Create default image
        img = Image.new('RGB', (64, 64), color='darkblue')
        from PIL import ImageDraw
        dc = ImageDraw.Draw(img)
        dc.rectangle([16, 16, 48, 48], fill='white')
        try:
            dc.text((27, 22), "T", fill='black')
        except:
            pass
    
    # Resize to multiple sizes for high-quality ICO
    sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    images = []
    
    for size in sizes:
        resized_img = img.resize(size, Image.Resampling.LANCZOS)
        images.append(resized_img)
    
    # Save as ICO with multiple sizes
    images[0].save(
        "trustnode_app_icon.ico",
        format="ICO",
        sizes=[(s[0], s[1]) for s in sizes],
        append_images=images[1:]
    )
    
    print("Created high-resolution application icon: trustnode_app_icon.ico")
    print(f"Icon includes sizes: {[f'{s[0]}x{s[1]}' for s in sizes]}")

if __name__ == "__main__":
    create_app_icon()