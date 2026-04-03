"""
Drives an ST7735S 128x128 LCD over SPI and provides:
  - Image rendering (PNG files scaled to 128x128)
  - 2D primitive drawing (rectangles, circles, lines, text)
  - Modular menu screen system with horizontal cycling (left/right)
  - Grasp selection with vertical cycling (up/down) and joystick confirm
  - Battery percentage readout (top-right, scaled 2.9V-4.1V)
  - Hand temperature readout (bottom-right, deg C)
  - Control mode indicator (POS / VEL / FOR)

Hardware wiring:
  - SPI MOSI : GPIO10    (shared ST7735 + MCP3008)
  - SPI SCLK : GPIO11    (shared ST7735 + MCP3008)
  - ST7735 CS : GPIO8
  - ST7735 DC : GPIO25
  - ST7735 RST: GPIO27
  - ST7735 BL : GPIO24   (backlight enable)

All inputs (joystick, buttons, ADC values) are provided by DSManager
through public methods - this module does NOT read GPIO directly.
"""

import time
import threading
from enum import Enum, auto
from dataclasses import dataclass, field
from typing import List, Optional, Callable

# ---------------------------------------------------------------------------
# Try importing hardware libraries; fall back to stubs for desktop testing
# ---------------------------------------------------------------------------
try:
    from PIL import Image, ImageDraw, ImageFont
    _HAS_PIL = True
except ImportError:
    _HAS_PIL = False

try:
    import ST7735
    _HAS_ST7735 = True
except ImportError:
    _HAS_ST7735 = False


# ============================= Constants ====================================

SCREEN_WIDTH = 128
SCREEN_HEIGHT = 128

# Battery voltage range for percentage mapping
BATTERY_V_MIN = 2.9
BATTERY_V_MAX = 4.1

# Colour palette (RGB tuples)
COL_BG        = (0, 0, 0)        # Black background
COL_TEXT       = (255, 255, 255)  # White text
COL_HIGHLIGHT  = (0, 200, 255)   # Cyan highlight for selected items
COL_MODE_POS   = (0, 255, 100)   # Green  - Position mode
COL_MODE_VEL   = (255, 200, 0)   # Yellow - Velocity mode
COL_MODE_FOR   = (255, 60, 60)   # Red    - Force mode
COL_BORDER     = (180, 180, 180) # Light grey for box outlines
COL_DIM_TEXT   = (120, 120, 120) # Dimmed text for non-selected items

# Layout geometry (pixel coordinates for 128x128 screen)
MODE_INDICATOR_X = 2
MODE_LABEL_Y     = [8, 52, 96]   # Y-centres for POS / VEL / FOR labels

MENU_AREA_X      = 30            # Left edge of centre menu area
MENU_AREA_W      = 68            # Width of centre menu column
MENU_CENTRE_Y    = 54            # Y-centre of the current-grasp box

BATTERY_X        = 98            # Top-right readout area
BATTERY_Y        = 2
TEMP_X           = 98            # Bottom-right readout area
TEMP_Y           = 112


# ============================= Enums ========================================

class ControlMode(Enum):
    POSITION = auto()
    VELOCITY = auto()
    FORCE = auto()


class JoystickAction(Enum):
    NONE   = auto()
    UP     = auto()
    DOWN   = auto()
    LEFT   = auto()
    RIGHT  = auto()
    SELECT = auto()


# ============================= Data Classes =================================

@dataclass
class MenuScreen:
    """Represents one horizontal menu page the user can cycle to."""
    name: str                              # Identifier shown on screen
    on_enter: Optional[Callable] = None    # Called when screen becomes active
    on_exit: Optional[Callable] = None     # Called when leaving this screen
    draw_custom: Optional[Callable] = None # Optional custom draw(ImageDraw, Image) callback


@dataclass
class DisplayState:
    """Mutable state shared between the update loop and DSManager."""
    # sensor values to be displayed
    battery_voltage: float = 3.7           # Volts, mapped to percentage
    hand_temperature: float = 25.0         # Degrees Celsius

    # current control mode
    control_mode: ControlMode = ControlMode.POSITION

    # list of grasps available to display in the menu
    grasp_names: List[str] = field(default_factory=lambda: ["Idle"])
    grasp_index: int = 0

    # creating a list for making menu screens
    menu_screens: List[MenuScreen] = field(default_factory=list)
    menu_index: int = 0

    # for starting and stopping the screen
    is_running: bool = True


# ============================= Display Driver ===============================

class DSDisplay:
    """
    High-level display controller for the DemoStick V3.

    Usage from DSManager:
        display = DSDisplay()
        display.start()

        # Push new sensor data any time:
        display.set_battery_voltage(3.85)
        display.set_hand_temperature(31.0)
        display.set_control_mode(ControlMode.VELOCITY)

        # Feed joystick / button events:
        display.handle_joystick(JoystickAction.DOWN)

        # Clean shutdown:
        display.stop()
    """

    def __init__(self, cs_pin, dc_pin, rst_pin, backlight_pin, use_hardware: bool = True, fps: int = 10):
        self._state = DisplayState()
        self._lock = threading.Lock()
        self._fps = fps
        self._render_thread: Optional[threading.Thread] = None

        # Grasp selection callback - DSManager can register a function that is called with the grasp filename string on joystick SELECT.
        self._on_grasp_selected: Optional[Callable[[str], None]] = None

        self._hw = None
        if use_hardware and _HAS_ST7735:
            try:
                self._hw = ST7735.ST7735(
                    port=0,
                    cs=cs_pin,                  # GPIO8
                    dc=dc_pin,                  # GPIO25
                    rst=rst_pin,                # GPIO27
                    backlight=backlight_pin,    # GPIO24
                    width=SCREEN_WIDTH,
                    height=SCREEN_HEIGHT,
                    rotation=0,
                    invert=False,
                    spi_speed_hz=24_000_000, #24mhz
                )
            except Exception as e:
                print(f"[DSDisplay] HW init failed: {e}  - running headless")
                self._hw = None

        # Frame buffer (PIL Image)
        if _HAS_PIL:
            self._fb = Image.new("RGB", (SCREEN_WIDTH, SCREEN_HEIGHT), COL_BG)
            self._draw = ImageDraw.Draw(self._fb) # draw the frame buffer
            self._font_sm = ImageFont.load_default()
            self._font_md = ImageFont.load_default()
            self._font_lg = ImageFont.load_default()
        else: # setup is wrong since no Pillow
            self._fb = None
            self._draw = None
            self._font_sm = self._font_md = self._font_lg = None

        # Register the default menu screens
        self._register_default_screens()

    # ------------------------------------------------------------------ #
    #  Public API - called by DSManager                                  #
    # ------------------------------------------------------------------ #

    def start(self):
        """Begin the render loop on a background thread"""
        with self._lock:
            self._state.is_running = True
        self._render_thread = threading.Thread(
            target=self._render_loop, daemon=True, name="DSDisplay-render"
        )
        self._render_thread.start()

    def stop(self):
        """Stop the render loop and blank the screen"""
        with self._lock:
            self._state.is_running = False
        if self._render_thread is not None:
            self._render_thread.join(timeout=2.0)
        self._clear_screen()
        if self._hw is not None:
            try:
                self._hw.set_backlight(0)
            except Exception:
                pass

    # <<<< Sensor setters >>>>

    def set_battery_voltage(self, voltage: float):
        """Update battery voltage (2.9 V - 4.1 V)."""
        with self._lock:
            self._state.battery_voltage = voltage

    def set_hand_temperature(self, temp_c: float):
        """Update hand temperature in deg C."""
        with self._lock:
            self._state.hand_temperature = temp_c

    def set_control_mode(self, mode: ControlMode):
        """Switch the displayed control mode (POS / VEL / FOR)."""
        with self._lock:
            self._state.control_mode = mode

    # <<<< Grasp list management >>>>

    def set_grasp_list(self, names: List[str]):
        """Replace the grasp list (e.g. after loading JSON files)."""
        with self._lock:
            self._state.grasp_names = list(names) if names else ["Idle"]
            self._state.grasp_index = 0

    def get_current_grasp(self) -> str:
        """Return the currently highlighted grasp name."""
        with self._lock:
            return self._state.grasp_names[self._state.grasp_index]

    # <<<< Joystick / input handling >>>>

    def handle_joystick(self, action: JoystickAction):
        """
        Process a single joystick event.
        UP/DOWN    -> cycle grasps
        LEFT/RIGHT -> cycle menu screens
        SELECT     -> confirm current grasp (fires callback)
        """
        grasp = None
        with self._lock:
            s = self._state
            if action == JoystickAction.UP:
                s.grasp_index = (s.grasp_index - 1) % len(s.grasp_names)

            elif action == JoystickAction.DOWN:
                s.grasp_index = (s.grasp_index + 1) % len(s.grasp_names)

            elif action == JoystickAction.LEFT:
                old = s.menu_index
                s.menu_index = (s.menu_index - 1) % len(s.menu_screens)
                self._fire_menu_transition(old, s.menu_index)

            elif action == JoystickAction.RIGHT:
                old = s.menu_index
                s.menu_index = (s.menu_index + 1) % len(s.menu_screens)
                self._fire_menu_transition(old, s.menu_index)

            elif action == JoystickAction.SELECT:
                grasp = s.grasp_names[s.grasp_index]

        # callback outside the lock to avoid deadlocks
        if grasp is not None and self._on_grasp_selected:
            self._on_grasp_selected(grasp)

    def register_grasp_callback(self, callback: Callable[[str], None]):
        """Register a function called with grasp name on SELECT."""
        self._on_grasp_selected = callback

    # <<<< Menu screen registration >>>>

    def add_menu_screen(self, screen: MenuScreen):
        """Append a new MenuScreen to the horizontal carousel."""
        with self._lock:
            self._state.menu_screens.append(screen)

    # not really needed
    # def insert_menu_screen(self, index: int, screen: MenuScreen):
    #     """Insert a MenuScreen at a specific position."""
    #     with self._lock:
    #         self._state.menu_screens.insert(index, screen)

    # <<<< Image display >>>>

    def show_image(self, path: str):
        """
        Load a PNG (or other PIL-supported format) and push it to screen.
        The image is resized to 128x128 with aspect-ratio letterboxing.
        """
        if not _HAS_PIL:
            return
        try:
            img = Image.open(path).convert("RGB")
            img = self._fit_image(img)
            with self._lock:
                self._fb.paste(img, (0, 0))
            self._push_framebuffer()
        except Exception as e:
            print(f"[DSDisplay] show_image error: {e}")

    # <<<< Primitive drawing (exposed to DSManager for custom screens) >>>>

    def draw_rect(self, x, y, w, h, *, outline=COL_BORDER, fill=None):
        if self._draw:
            self._draw.rectangle([x, y, x + w, y + h], outline=outline, fill=fill)

    def draw_circle(self, cx, cy, r, *, outline=COL_BORDER, fill=None):
        if self._draw:
            self._draw.ellipse([cx - r, cy - r, cx + r, cy + r],
                               outline=outline, fill=fill)

    def draw_line(self, x1, y1, x2, y2, *, fill=COL_TEXT, width=1):
        if self._draw:
            self._draw.line([x1, y1, x2, y2], fill=fill, width=width)

    def draw_text(self, x, y, text, *, fill=COL_TEXT, font=None, anchor=None):
        if self._draw:
            self._draw.text((x, y), text, fill=fill,
                            font=font or self._font_sm, anchor=anchor)

    def get_framebuffer(self):
        """Return the raw PIL Image for advanced custom drawing."""
        return self._fb

    def get_draw_context(self):
        """Return the ImageDraw object for advanced custom drawing."""
        return self._draw

    def flush(self):
        """Manually push the current framebuffer to the LCD."""
        self._push_framebuffer()

#
#       Internal Rendering
#       - Everything below here is related to drawing the screen and building the main runtime menu.
#       - Each object on the screen is built up by some function, which is then rolled up _compose_frame 
#
    def _render_loop(self):
        """Background thread: redraw the screen at the target FPS."""
        interval = 1.0 / self._fps
        while True:
            with self._lock:
                if not self._state.is_running:
                    break
                snap = self._snapshot()

            self._compose_frame(snap)
            self._push_framebuffer()
            time.sleep(interval)


    def _snapshot(self) -> dict:
        """Take a lightweight copy of state for rendering (must hold lock)."""
        s = self._state
        return {
            "battery_v": s.battery_voltage,
            "temp_c": s.hand_temperature,
            "mode": s.control_mode,
            "grasps": list(s.grasp_names),
            "grasp_idx": s.grasp_index,
            "screens": list(s.menu_screens),
            "screen_idx": s.menu_index,
        }

    def _compose_runtime_frame(self, snap: dict):
        """Draw the entire UI frame for runtime grasp and mode selection into the PIL framebuffer."""
        if not _HAS_PIL:
            return

        d = self._draw
        # Clear
        d.rectangle([0, 0, SCREEN_WIDTH - 1, SCREEN_HEIGHT - 1], fill=COL_BG)

        # Check if active screen has a full custom draw
        screens = snap["screens"]
        if screens:
            active_screen = screens[snap["screen_idx"]]
            if active_screen.draw_custom is not None:
                active_screen.draw_custom(d, self._fb)
                # Still overlay battery + temp on custom screens
                self._draw_battery(d, snap["battery_v"])
                self._draw_temperature(d, snap["temp_c"])
                return

        # Control-mode column (left side) 
        self._draw_mode_indicators(d, snap["mode"])

        # Centre menu area 
        self._draw_grasp_carousel(d, snap)

        # Menu screen name (bottom-left) 
        if screens:
            screen_name = screens[snap["screen_idx"]].name
            d.text((MENU_AREA_X, 1), screen_name,
                   fill=COL_DIM_TEXT, font=self._font_sm)

        # Battery (top-right)
        self._draw_battery(d, snap["battery_v"])

        # Temperature (bottom-right)
        self._draw_temperature(d, snap["temp_c"])

    # <<<< Sub-renderers >>>>

    def _draw_mode_indicators(self, d: "ImageDraw.Draw", mode: ControlMode):
        """Draw POS / VEL / FOR indicators on the left column."""
        labels = [
            (ControlMode.POSITION, "POS", COL_MODE_POS),
            (ControlMode.VELOCITY, "VEL", COL_MODE_VEL),
            (ControlMode.FORCE,    "FOR", COL_MODE_FOR),
        ]
        radius = 11
        cx = MODE_INDICATOR_X + radius
        for i, (m, label, colour) in enumerate(labels):
            cy = MODE_LABEL_Y[i] + radius
            is_active = (mode == m)
            outline_col = colour if is_active else COL_DIM_TEXT
            fill_col = colour if is_active else None
            d.ellipse([cx - radius, cy - radius, cx + radius, cy + radius],
                      outline=outline_col, fill=fill_col)
            text_col = COL_BG if is_active else COL_DIM_TEXT
            bbox = d.textbbox((0, 0), label, font=self._font_sm)
            tw = bbox[2] - bbox[0]
            th = bbox[3] - bbox[1]
            d.text((cx - tw // 2, cy - th // 2), label,
                   fill=text_col, font=self._font_sm)

    def _draw_grasp_carousel(self, d: "ImageDraw.Draw", snap: dict):
        """Draw PREV / CURRENT / NEXT grasp boxes in the centre column."""
        grasps = snap["grasps"]
        idx = snap["grasp_idx"]
        n = len(grasps)

        box_w = MENU_AREA_W
        box_h = 22
        cx = MENU_AREA_X + box_w // 2

        # Previous grasp (top)
        prev_idx = (idx - 1) % n
        prev_name = grasps[prev_idx]
        self._draw_label_box(d, MENU_AREA_X, 14, box_w, box_h,
                             prev_name, COL_DIM_TEXT, self._font_sm)
        # Up arrow
        arrow_cx = cx
        d.polygon([(arrow_cx, 12), (arrow_cx - 4, 17), (arrow_cx + 4, 17)],
                  fill=COL_DIM_TEXT)

        # Current grasp (centre) - highlighted
        self._draw_label_box(d, MENU_AREA_X, MENU_CENTRE_Y - box_h // 2,
                             box_w, box_h + 4,
                             grasps[idx], COL_HIGHLIGHT, self._font_md)

        # Next grasp (bottom)
        next_idx = (idx + 1) % n
        next_name = grasps[next_idx]
        self._draw_label_box(d, MENU_AREA_X, 88, box_w, box_h,
                             next_name, COL_DIM_TEXT, self._font_sm)
        # Down arrow
        d.polygon([(arrow_cx, 114), (arrow_cx - 4, 109), (arrow_cx + 4, 109)],
                  fill=COL_DIM_TEXT)

        # Left / right screen indicators
        if len(snap["screens"]) > 1:
            d.text((MENU_AREA_X - 6, MENU_CENTRE_Y - 4), "<",
                   fill=COL_DIM_TEXT, font=self._font_sm)
            d.text((MENU_AREA_X + box_w + 2, MENU_CENTRE_Y - 4), ">",
                   fill=COL_DIM_TEXT, font=self._font_sm)

    def _draw_label_box(self, d, x, y, w, h, text, colour, font):
        """Draw a box with centred label."""
        d.rectangle([x, y, x + w, y + h], outline=colour)
        bbox = d.textbbox((0, 0), text, font=font)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        tx = x + (w - tw) // 2
        ty = y + (h - th) // 2
        d.text((tx, ty), text, fill=colour, font=font)

    def _draw_battery(self, d: "ImageDraw.Draw", voltage: float):
        """Render battery percentage in the top-right corner."""
        pct = self._voltage_to_percent(voltage)
        label = f"{pct}%"
        d.text((BATTERY_X, BATTERY_Y), "BATT", fill=COL_DIM_TEXT,
               font=self._font_sm)
        d.text((BATTERY_X, BATTERY_Y + 10), label, fill=COL_TEXT,
               font=self._font_md)

    def _draw_temperature(self, d: "ImageDraw.Draw", temp_c: float):
        """Render hand temperature in the bottom-right corner."""
        label = f"{temp_c:.0f}C"
        d.text((TEMP_X, TEMP_Y), "TEMP", fill=COL_DIM_TEXT,
               font=self._font_sm)
        d.text((TEMP_X, TEMP_Y + 10), label, fill=COL_TEXT,
               font=self._font_md)

    # <<<< Helpers >>>>

    @staticmethod
    def _voltage_to_percent(v: float) -> int:
        """Map battery voltage to 0-100 % (linear between 2.9 V and 4.1 V)."""
        pct = (v - BATTERY_V_MIN) / (BATTERY_V_MAX - BATTERY_V_MIN) * 100.0
        return max(0, min(100, int(round(pct))))

    def _fit_image(self, img: "Image.Image") -> "Image.Image":
        """Resize image to fit 128x128 with letterboxing."""
        img.thumbnail((SCREEN_WIDTH, SCREEN_HEIGHT), Image.LANCZOS)
        canvas = Image.new("RGB", (SCREEN_WIDTH, SCREEN_HEIGHT), COL_BG)
        offset_x = (SCREEN_WIDTH - img.width) // 2
        offset_y = (SCREEN_HEIGHT - img.height) // 2
        canvas.paste(img, (offset_x, offset_y))
        return canvas

    def _push_framebuffer(self):
        """Send the PIL framebuffer to the physical display."""
        if self._hw is not None and self._fb is not None:
            try:
                self._hw.display(self._fb)
            except Exception as e:
                print(f"[DSDisplay] push error: {e}")

    def _clear_screen(self):
        """Fill the screen black."""
        if _HAS_PIL and self._draw:
            self._draw.rectangle(
                [0, 0, SCREEN_WIDTH - 1, SCREEN_HEIGHT - 1], fill=COL_BG
            )
            self._push_framebuffer()

    def _fire_menu_transition(self, old_idx: int, new_idx: int):
        """Call on_exit / on_enter hooks when menu screen changes (lock held)."""
        screens = self._state.menu_screens
        if screens[old_idx].on_exit:
            try:
                screens[old_idx].on_exit()
            except Exception:
                pass
        if screens[new_idx].on_enter:
            try:
                screens[new_idx].on_enter()
            except Exception:
                pass

    def _register_default_screens(self):
        """Set up the built-in menu screens."""
        self._state.menu_screens = [
            MenuScreen(name="Grasps"),       # Default grasp selection
            MenuScreen(name="Idle"),         # Idle / home screen
            MenuScreen(name="Settings"),     # Placeholder for settings
        ]


# # ============================= Standalone Demo ==============================

# if __name__ == "__main__":
#     """
#     Desktop demo - renders a single frame to a PNG so you can preview the UI
#     without any Raspberry Pi hardware attached.
#     """
#     if not _HAS_PIL:
#         print("PIL/Pillow is required for the demo.  pip install Pillow")
#         raise SystemExit(1)

#     display = DSDisplay(use_hardware=False)

#     # Simulate DSManager providing data
#     display.set_grasp_list(["Idle", "Power", "Pinch", "Tripod", "Lateral", "Hook"])
#     display.set_battery_voltage(3.73)      # ~69 %
#     display.set_hand_temperature(31.0)
#     display.set_control_mode(ControlMode.POSITION)

#     # Move grasp selection down twice to show "Pinch" as current
#     display.handle_joystick(JoystickAction.DOWN)
#     display.handle_joystick(JoystickAction.DOWN)

#     # Render one frame manually
#     with display._lock:
#         snap = display._snapshot()
#     display._compose_runtime_frame(snap)

#     output_path = "/home/claude/ds_display_preview.png"
#     display._fb.save(output_path)
#     print(f"Preview saved to {output_path}")

#     # Also demonstrate image overlay
#     print("\nPublic API summary:")
#     print("  set_battery_voltage(v)        - float, 2.9-4.1 V")
#     print("  set_hand_temperature(c)       - float, deg C")
#     print("  set_control_mode(mode)        - ControlMode enum")
#     print("  set_grasp_list(names)         - list of grasp name strings")
#     print("  handle_joystick(action)       - JoystickAction enum")
#     print("  register_grasp_callback(fn)   - fn(grasp_name) on SELECT")
#     print("  add_menu_screen(screen)       - MenuScreen dataclass")
#     print("  show_image(path)              - PNG overlay")
#     print("  start() / stop()              - render thread lifecycle")