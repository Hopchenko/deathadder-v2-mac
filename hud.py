"""The DPI pop-up: a small overlay like macOS's volume pop-up, shown when you press the mouse's DPI button.
Main thread only."""
from AppKit import (NSAccessibilityAnnouncementKey, NSAppearance, NSAppearanceNameDarkAqua, NSAccessibilityAnnouncementRequestedNotification,
                    NSAccessibilityPostNotificationWithUserInfo, NSAnimationContext, NSApp, NSBackingStoreBuffered,
                    NSColor, NSEvent, NSFont, NSMakeRect, NSPanel, NSPointInRect, NSScreen, NSStatusWindowLevel,
                    NSTextAlignmentCenter, NSTextField, NSView, NSVisualEffectView,
                    NSWindowCollectionBehaviorCanJoinAllSpaces, NSWindowCollectionBehaviorFullScreenAuxiliary,
                    NSWindowCollectionBehaviorStationary, NSWindowStyleMaskBorderless,
                    NSWindowStyleMaskNonactivatingPanel)
from PyObjCTools import AppHelper
import Quartz  # noqa: F401  (lets PyObjC pass CGColor to layers)

W, H = 200, 120
SHOW_SECONDS = 1.2
GREEN = NSColor.colorWithSRGBRed_green_blue_alpha_(0.267, 0.839, 0.173, 1)


def _label(size, weight, color):
    t = NSTextField.labelWithString_("")
    t.setFont_(NSFont.monospacedDigitSystemFontOfSize_weight_(size, weight))
    t.setTextColor_(color)
    t.setAlignment_(NSTextAlignmentCenter)
    return t


class Hud:
    def __init__(self):
        p = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, W, H), NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel,
            NSBackingStoreBuffered, False)
        p.setLevel_(NSStatusWindowLevel)                # above normal windows, like the system pop-ups
        p.setOpaque_(False)
        p.setBackgroundColor_(NSColor.clearColor())
        p.setIgnoresMouseEvents_(True)                  # never steals a click
        p.setHasShadow_(True)
        p.setCollectionBehavior_(NSWindowCollectionBehaviorCanJoinAllSpaces | NSWindowCollectionBehaviorStationary
                                 | NSWindowCollectionBehaviorFullScreenAuxiliary)   # also over full-screen apps
        bg = NSVisualEffectView.alloc().initWithFrame_(NSMakeRect(0, 0, W, H))
        bg.setMaterial_(13)                             # NSVisualEffectMaterialHUDWindow
        bg.setState_(1)                                 # always active
        bg.setAppearance_(NSAppearance.appearanceNamed_(NSAppearanceNameDarkAqua))
        bg.setWantsLayer_(True)
        bg.layer().setCornerRadius_(18)
        bg.layer().setMasksToBounds_(True)
        # a dark tint under the text keeps it readable over bright wallpapers (the blur view ignores its own
        # layer colour, so the tint is a separate view on top of it)
        tint = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, W, H))
        tint.setWantsLayer_(True)
        tint.layer().setBackgroundColor_(NSColor.colorWithWhite_alpha_(0.0, 0.62).CGColor())
        bg.addSubview_(tint)
        self.number = _label(40, 0.4, NSColor.whiteColor())
        self.number.setFrame_(NSMakeRect(0, 52, W, 50))
        self.caption = _label(13, 0.0, NSColor.colorWithWhite_alpha_(1.0, 0.88))
        self.caption.setFrame_(NSMakeRect(0, 36, W, 18))
        self.dots = NSView.alloc().initWithFrame_(NSMakeRect(0, 14, W, 14))
        for v in (self.number, self.caption, self.dots):
            bg.addSubview_(v)
        p.setContentView_(bg)
        self.panel = p
        self.gen = 0

    def show(self, dpi, active, count):
        self.number.setStringValue_(f"{dpi}")
        self.caption.setStringValue_(f"DPI · stage {active} of {count}" if count else "DPI")
        self._dots(active, count)
        mouse = NSEvent.mouseLocation()                 # the screen you're looking at, not always the main one
        screen = next((s for s in NSScreen.screens() if NSPointInRect(mouse, s.frame())), NSScreen.mainScreen())
        f = screen.visibleFrame()
        self.panel.setFrameOrigin_((f.origin.x + (f.size.width - W) / 2, f.origin.y + f.size.height * 0.18))
        self.panel.setAlphaValue_(1.0)
        self.panel.orderFrontRegardless()
        # VoiceOver users hear the new value too
        NSAccessibilityPostNotificationWithUserInfo(NSApp, NSAccessibilityAnnouncementRequestedNotification,
                                                    {NSAccessibilityAnnouncementKey: f"{dpi} DPI, stage {active}"})
        self.gen += 1
        gen = self.gen
        AppHelper.callLater(SHOW_SECONDS, lambda: self._fade(gen))

    def _dots(self, active, count):
        for v in list(self.dots.subviews()):
            v.removeFromSuperview()
        size, gap = 8, 8
        x = (W - (count * size + (count - 1) * gap)) / 2
        for i in range(1, count + 1):
            d = NSView.alloc().initWithFrame_(NSMakeRect(x, 3, size, size))
            d.setWantsLayer_(True)
            d.layer().setCornerRadius_(size / 2)
            d.layer().setBackgroundColor_((GREEN if i == active else NSColor.colorWithWhite_alpha_(1.0, 0.35)).CGColor())
            self.dots.addSubview_(d)
            x += size + gap

    def _fade(self, gen):
        if gen != self.gen:                             # pressed again: keep showing the newer value
            return
        NSAnimationContext.beginGrouping()
        NSAnimationContext.currentContext().setDuration_(0.35)
        self.panel.animator().setAlphaValue_(0.0)
        NSAnimationContext.endGrouping()
        AppHelper.callLater(0.4, lambda: gen == self.gen and self.panel.orderOut_(None))
