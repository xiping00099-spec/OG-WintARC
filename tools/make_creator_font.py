"""
Rebuilds the small font file used for the creator's small-capital letters (web_static/arc-latin.woff).
Needs: pip install fonttools. Source: DejaVu Sans Bold (free licence, see fonts/DEJAVU-LICENSE.txt).
Only needed if the creator name in config.py starts using other rare characters.   python tools/make_creator_font.py
"""
from fontTools import subset

opts = subset.Options(); opts.flavor = "woff"; opts.layout_features = []; opts.name_IDs = [1, 2]; opts.prune_unicode_ranges = False
font = subset.load_font("fonts/DejaVuSans-Bold.ttf", opts)
sub = subset.Subsetter(opts); sub.populate(unicodes=[0x0274, 0x0280, 0x1D00, 0x1D07]); sub.subset(font)
subset.save_font(font, "web_static/arc-latin.woff", opts)
print("wrote web_static/arc-latin.woff")
