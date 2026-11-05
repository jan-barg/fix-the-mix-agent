// Rotating progress lines under the plain stage label (SPEC §2.4). The stage says what is running;
// these just keep the wait company. Keyed by what is running, with a general pool for everything else.
export const PROGRESS_MESSAGES = {
  general: [
    "Asking the DJ for Track ID",
    "Dancing my ass off",
    "Crying in the club rn",
    "Plugging it through an analog speaker",
    "Nodding along at the right BPM",
    "Squinting at a spectrogram",
    "Reading the meters twice, to be sure",
    "Comparing notes with your references",
    "Resisting the urge to turn it up",
  ],
  kick: [
    "Soloing the kick. Again.",
    "Counting kicks on my fingers",
    "Checking how long that tail really is",
    "Listening for the click on top",
  ],
  low_end: [
    "Feeling the bass thump in the chest",
    "Watching the bass duck out of the kick's way",
    "Checking if the sub survives a phone speaker",
    "Timing the sidechain pump",
  ],
  tonal_loudness: [
    "Arguing with the limiter",
    "Holding a ruler up to the spectrum",
    "Measuring how hard the drop lands",
    "Matching peaks, not LUFS. On purpose.",
  ],
  stereo: [
    "Mono-Stereo-Mono-Stereo... ARGHHHHHHH",
    "Folding it to mono to see what falls out",
    "Walking from the left speaker to the right one",
    "Checking the club rig won't eat the sub",
  ],
  lead: [
    "Weighing what the researchers found",
    "Deciding what matters most for your goals",
    "Double-checking a suspicious number",
  ],
};
