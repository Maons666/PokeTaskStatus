# Third-party assets

PokeTask Status is an unofficial fan utility and is not affiliated with Nintendo, Creatures Inc., GAME FREAK inc., Anthropic, or OpenAI.

## Pokémon game sprites

Metagross (#376) front/back animated sprites and Unown question/exclamation forms from Pokémon Black/White are original game artwork. Inventory sprites include Poké Ball, Potion, Rare Candy, Metal Coat, Poké Flute, Coin Case, Poké Radar, Town Map, Vs. Recorder, Silph Scope and Escape Rope. Source images are downloaded from the community [PokeAPI sprites mirror](https://github.com/PokeAPI/sprites) without AI generation. Nintendo / Creatures Inc. / GAME FREAK inc. retain rights to Pokémon artwork and characters. The mirror is not the rights holder and its repository license is not a grant of rights to Pokémon artwork.

`assets/manifest.json` pins the upstream revision and records individual paths and hashes. Downloaded binaries are Git-ignored. Idle motion plays the original GIF frames. The custom Meteor Mash and completion animations separate, rotate and stretch the original foreleg pixels with nearest-neighbor sampling, with a small program-drawn shoulder joint, particles and sign. Meteor Mash lifts and holds the screen-right fist with a slight tremble, then sweeps it left, leaving one recolored, pixel-dithered silhouette of the original limb along a short drawn arc. The occluded far leg is reconstructed from scaled original leg/claw pixels and the source palette, layered behind the body and moving foreleg; these hidden pixels are an adaptation, not additional frames extracted from the game. Speech bubbles and interface panels are independently drawn. Original Unown forms appear inside status bubbles; inventory artwork is used only in menus. This choreography is not an original game animation. The single-file build embeds the downloaded assets; this does not change their ownership or grant distribution permission.

## Pixel font

[Fusion Pixel Font](https://github.com/TakWolf/fusion-pixel-font), 12px monospaced Simplified Chinese, under the SIL Open Font License 1.1. The pinned release is recorded in the manifest. Its license files are downloaded into `assets/vendor/` and included in the executable alongside the font.

## Runtime

Python, Tcl/Tk, Pillow and PyInstaller retain their respective licenses. PyInstaller's bootloader exception permits packaging applications; no application source license is implied for the third-party game assets. The build uses the installed runtime distributions and includes their standard bundled notices.
