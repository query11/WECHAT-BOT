using System;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Runtime.InteropServices;
using System.Windows.Forms;

public static class TrayDesign {
    public static readonly Color Blue=Color.FromArgb(74,144,226);
    public static readonly Color TextColor=Color.FromArgb(44,62,80);
    public static readonly Color MutedColor=Color.FromArgb(149,165,166);
    public static readonly Color CardColor=Color.FromArgb(248,249,250);
    public static readonly Color BorderColor=Color.FromArgb(229,234,238);
    [DllImport("user32.dll")] static extern bool SetProcessDpiAwarenessContext(IntPtr value);
    public static void EnableDpi() { try { SetProcessDpiAwarenessContext(new IntPtr(-4)); } catch {} }
    public static GraphicsPath Round(RectangleF r, float radius) {
        var p = new GraphicsPath(); float d = Math.Min(radius * 2,Math.Min(r.Width,r.Height));
        if(d<=0)return p;
        p.AddArc(r.X,r.Y,d,d,180,90); p.AddArc(r.Right-d,r.Y,d,d,270,90);
        p.AddArc(r.Right-d,r.Bottom-d,d,d,0,90); p.AddArc(r.X,r.Bottom-d,d,d,90,90); p.CloseFigure(); return p;
    }
    public static Bitmap Symbol(Color color, int size, string state) {
        var b = new Bitmap(size,size);
        using(var g = Graphics.FromImage(b)) {
            g.SmoothingMode=SmoothingMode.AntiAlias; g.ScaleTransform(size/64f,size/64f);
            using(var bg=new SolidBrush(color))
            using(var p=Round(new RectangleF(3,3,58,58),15)) g.FillPath(bg,p);
            using(var rear=new SolidBrush(Color.FromArgb(185,255,255,255)))
            using(var p=Round(new RectangleF(27,27,26,21),6)) g.FillPath(rear,p);
            using(var rear=new SolidBrush(Color.FromArgb(185,255,255,255))) g.FillPolygon(rear,new[]{new PointF(43,45),new PointF(48,52),new PointF(48,43)});
            using(var p=Round(new RectangleF(12,14,34,25),7)) g.FillPath(Brushes.White,p);
            g.FillPolygon(Brushes.White,new[]{new PointF(17,35),new PointF(17,45),new PointF(28,37)});
            using(var pen=new Pen(color,2.5f)) {
                pen.StartCap=pen.EndCap=LineCap.Round;
                if(state=="error" || state=="error-stale") {g.DrawLine(pen,25,22,33,31);g.DrawLine(pen,33,22,25,31);}
                else if(state=="reconnecting" || state=="stopping" || state=="draining") {g.DrawLine(pen,29,20,29,27);g.DrawLine(pen,29,31,29,31.5f);}
                else if(state=="stopped") {g.DrawLine(pen,23,27,35,27);}
                else {g.DrawLine(pen,20,23,38,23);g.DrawLine(pen,20,30,32,30);}
            }
        }
        return b;
    }
    public static Bitmap Glyph(string kind, Color color, int size) {
        var b=new Bitmap(size,size);
        using(var g=Graphics.FromImage(b)) {g.SmoothingMode=SmoothingMode.AntiAlias;g.ScaleTransform(size/24f,size/24f);DrawGlyph(g,kind,color);}
        return b;
    }
    public static void DrawGlyph(Graphics g, string kind, Color color) {
        using(var pen=new Pen(color,1.6f)) {
            pen.StartCap=pen.EndCap=LineCap.Round;pen.LineJoin=LineJoin.Round;
            if(kind=="apps") {
                foreach(var r in new[]{new RectangleF(3,3,7,7),new RectangleF(14,3,7,7),new RectangleF(3,14,7,7),new RectangleF(14,14,7,7)})
                    using(var p=Round(r,1.5f)) g.DrawPath(pen,p);
            } else if(kind=="clock") {
                g.DrawEllipse(pen,3,3,18,18);g.DrawLines(pen,new[]{new PointF(12,7),new PointF(12,12),new PointF(16,12)});
            } else if(kind=="chat") {
                using(var p=Round(new RectangleF(3,4,18,14),4))g.DrawPath(pen,p);
                g.DrawLines(pen,new[]{new PointF(7,18),new PointF(5,21),new PointF(12,18)});
                g.DrawLine(pen,8,9,16,9);g.DrawLine(pen,8,13,13,13);
            } else if(kind=="send") {
                g.DrawPolygon(pen,new[]{new PointF(3,10),new PointF(21,3),new PointF(14,21),new PointF(10,14)});
                g.DrawLine(pen,10,14,21,3);
            } else if(kind=="cube") {
                g.DrawPolygon(pen,new[]{new PointF(12,2),new PointF(21,7),new PointF(21,17),new PointF(12,22),new PointF(3,17),new PointF(3,7)});
                g.DrawLines(pen,new[]{new PointF(3,7),new PointF(12,12),new PointF(21,7)});g.DrawLine(pen,12,12,12,22);
            } else if(kind=="activity") {
                g.DrawLines(pen,new[]{new PointF(2,12),new PointF(6,12),new PointF(9,4),new PointF(14,20),new PointF(17,12),new PointF(22,12)});
            } else if(kind=="shield") {
                g.DrawPolygon(pen,new[]{new PointF(12,2),new PointF(21,6),new PointF(20,15),new PointF(17,19),new PointF(12,22),new PointF(7,19),new PointF(4,15),new PointF(3,6)});
                g.DrawLines(pen,new[]{new PointF(8,11),new PointF(11,14),new PointF(16,9)});
            } else if(kind=="chevron") {
                g.DrawLines(pen,new[]{new PointF(9,5),new PointF(16,12),new PointF(9,19)});
            } else if(kind=="check") {
                g.DrawLines(pen,new[]{new PointF(5,12),new PointF(10,17),new PointF(19,7)});
            } else if(kind=="empty-task") {
                g.DrawLines(pen,new[]{new PointF(15,3),new PointF(5,3),new PointF(5,21),new PointF(19,21),new PointF(19,7),new PointF(15,3),new PointF(15,8),new PointF(19,8)});
                g.DrawLine(pen,8,12,15,12);g.DrawLine(pen,8,16,12,16);
            } else if(kind=="restart") {
                g.DrawArc(pen,3,3,18,18,35,285);g.DrawLines(pen,new[]{new PointF(16,3),new PointF(21,3),new PointF(21,8)});
            } else if(kind=="settings") {
                g.DrawEllipse(pen,4,4,16,16);g.DrawEllipse(pen,9,9,6,6);
                for(int i=0;i<8;i++){double a=i*Math.PI/4;g.DrawLine(pen,12+(float)Math.Cos(a)*8,12+(float)Math.Sin(a)*8,12+(float)Math.Cos(a)*10.5f,12+(float)Math.Sin(a)*10.5f);}
            } else if(kind=="history") {
                g.DrawArc(pen,4,3,17,18,205,305);g.DrawLines(pen,new[]{new PointF(3,4),new PointF(3,9),new PointF(8,9)});
                g.DrawLines(pen,new[]{new PointF(12,7),new PointF(12,12),new PointF(16,14)});
            } else if(kind=="power") {
                g.DrawArc(pen,4,4,16,17,-45,270);g.DrawLine(pen,12,2,12,11);
            } else if(kind=="stop") {
                using(var p=Round(new RectangleF(6,6,12,12),1.5f))g.DrawPath(pen,p);
            } else if(kind=="log") {
                using(var p=Round(new RectangleF(4,3,16,18),2))g.DrawPath(pen,p);
                for(int y=8;y<=16;y+=4){g.DrawLine(pen,8,y,8.2f,y);g.DrawLine(pen,11,y,16,y);}
            } else if(kind=="task") {
                g.DrawLines(pen,new[]{new PointF(8,4),new PointF(5,4),new PointF(5,21),new PointF(19,21),new PointF(19,4),new PointF(16,4)});
                using(var p=Round(new RectangleF(8,2,8,4),1))g.DrawPath(pen,p);
                g.DrawLine(pen,9,11,15,11);g.DrawLine(pen,9,15,15,15);
            } else {
                using(var p=Round(new RectangleF(5,3,14,18),2))g.DrawPath(pen,p);
                g.DrawLine(pen,9,8,15,8);g.DrawLine(pen,9,12,15,12);g.DrawLine(pen,9,16,13,16);
            }
        }
    }
    public static void StyleFields(Control root) {
        foreach(Control control in root.Controls) {
            if(control is TextBox || control is ComboBox || control is ListView) {
                control.Font=new Font("Microsoft YaHei UI",9.5f);control.ForeColor=TextColor;control.BackColor=Color.White;
                var text=control as TextBox;if(text!=null)text.BorderStyle=BorderStyle.FixedSingle;
                var list=control as ListView;if(list!=null){list.BorderStyle=BorderStyle.FixedSingle;list.GridLines=false;}
            }
            if(control.HasChildren)StyleFields(control);
        }
    }
    public static void SaveShortcut(Form window, Button save) {
        window.KeyPreview=true;
        window.KeyDown+=(sender,e)=>{if(e.Control && e.KeyCode==Keys.S){e.SuppressKeyPress=true;e.Handled=true;if(save.Enabled)save.PerformClick();}};
    }
    public static Color SurfaceFor(Control control) {
        for(var current=control;current!=null;current=current.Parent) {
            var card=current as TrayCard;
            if(card!=null)return card.Surface;
            if(current.BackColor.A==255)return current.BackColor;
        }
        return Color.White;
    }
    // Opt in per window so the shared settings dialogs keep their existing styling.
    public static void EnableDepth(Control root) {
        var card=root as TrayCard;if(card!=null)card.Depth=true;
        var button=root as TrayButton;if(button!=null)button.Depth=true;
        foreach(Control child in root.Controls)EnableDepth(child);
    }
    public static Color Mix(Color color, Color target, float amount) {
        return Color.FromArgb((int)(color.R+(target.R-color.R)*amount),(int)(color.G+(target.G-color.G)*amount),(int)(color.B+(target.B-color.B)*amount));
    }
    public static void SoftShadow(Graphics graphics, RectangleF face, float radius, float scale, Color tint, int opacity) {
        // Three faint layers stay inside the control bounds; no extra layout space is needed.
        for(int layer=3;layer>=1;layer--) {
            var shadow=face;shadow.Offset(0,layer*scale);
            using(var path=Round(shadow,radius))
            using(var brush=new SolidBrush(Color.FromArgb(opacity,tint)))graphics.FillPath(brush,path);
        }
    }
}
public class TrayBadge : Control {
    public Color StatusColor {get;set;}
    public bool Navigate {get;set;}
    public TrayBadge(){DoubleBuffered=true;SetStyle(ControlStyles.ResizeRedraw,true);StatusColor=TrayDesign.Blue;Font=new Font("Microsoft YaHei UI",9);}
    protected override void OnPaint(PaintEventArgs e){
        e.Graphics.Clear(TrayDesign.SurfaceFor(Parent));e.Graphics.SmoothingMode=SmoothingMode.AntiAlias;float scale=e.Graphics.DpiX/96f;
        using(var p=TrayDesign.Round(new RectangleF(0,0,Width-1,Height-1),Height/2f))
        using(var brush=new SolidBrush(Color.FromArgb(13,StatusColor)))e.Graphics.FillPath(brush,p);
        using(var brush=new SolidBrush(StatusColor))e.Graphics.FillEllipse(brush,12*scale,(Height-7*scale)/2,7*scale,7*scale);
        TextRenderer.DrawText(e.Graphics,Text,Font,new Rectangle((int)(27*scale),0,Math.Max(0,Width-(int)((Navigate?52:34)*scale)),Height),StatusColor,TextFormatFlags.Left|TextFormatFlags.VerticalCenter|TextFormatFlags.EndEllipsis);
        if(Navigate){var saved=e.Graphics.Save();e.Graphics.TranslateTransform(Width-28*scale,(Height-16*scale)/2);e.Graphics.ScaleTransform(16*scale/24,16*scale/24);TrayDesign.DrawGlyph(e.Graphics,"chevron",StatusColor);e.Graphics.Restore(saved);}
    }
}
public class TrayCard : Panel {
    public Color Surface {get;set;}
    public Color Border {get;set;}
    public bool Depth {get;set;}
    public TrayCard() { DoubleBuffered=true;SetStyle(ControlStyles.ResizeRedraw,true); BackColor=Color.Transparent; Surface=TrayDesign.CardColor; Border=TrayDesign.BorderColor; }
    protected override void OnPaintBackground(PaintEventArgs e) {
        e.Graphics.Clear(TrayDesign.SurfaceFor(Parent)); e.Graphics.SmoothingMode=SmoothingMode.AntiAlias;
        float scale=e.Graphics.DpiX/96f;
        var face=new RectangleF(.5f,.5f,Width-1,Math.Max(1,Height-1-(Depth?3*scale:0)));
        if(Depth)TrayDesign.SoftShadow(e.Graphics,face,12*scale,scale,Color.FromArgb(46,68,94),8);
        using(var p=TrayDesign.Round(face,12*scale)) {
            using(var brush=new SolidBrush(Surface)) e.Graphics.FillPath(brush,p);
            using(var pen=new Pen(Border)) e.Graphics.DrawPath(pen,p);
        }
        if(Depth) {
            face.Inflate(-scale,-scale);
            using(var p=TrayDesign.Round(face,11*scale))
            using(var pen=new Pen(Color.FromArgb(210,255,255,255),scale))e.Graphics.DrawPath(pen,p);
        }
    }
}
public class TrayButton : Button {
    public bool Primary {get;set;}
    public bool Quiet {get;set;}
    public bool Danger {get;set;}
    public bool Navigate {get;set;}
    public bool ToggleChip {get;set;}
    public bool Depth {get;set;}
    public string Description {get;set;}
    public string IconKind {get;set;}
    bool hover;
    bool pressed;
    public TrayButton() { SetStyle(ControlStyles.UserPaint|ControlStyles.AllPaintingInWmPaint|ControlStyles.OptimizedDoubleBuffer|ControlStyles.ResizeRedraw,true); Cursor=Cursors.Hand; ForeColor=TrayDesign.TextColor; }
    protected override void OnMouseEnter(EventArgs e){hover=true;Invalidate();base.OnMouseEnter(e);}
    protected override void OnMouseLeave(EventArgs e){hover=false;Invalidate();base.OnMouseLeave(e);}
    protected override void OnMouseDown(MouseEventArgs e){if(e.Button==MouseButtons.Left)pressed=true;Invalidate();base.OnMouseDown(e);}
    protected override void OnMouseUp(MouseEventArgs e){pressed=false;Invalidate();base.OnMouseUp(e);}
    protected override void OnMouseCaptureChanged(EventArgs e){if(!Capture)pressed=false;Invalidate();base.OnMouseCaptureChanged(e);}
    protected override void OnKeyDown(KeyEventArgs e){if(e.KeyCode==Keys.Space)pressed=true;Invalidate();base.OnKeyDown(e);}
    protected override void OnKeyUp(KeyEventArgs e){if(e.KeyCode==Keys.Space)pressed=false;Invalidate();base.OnKeyUp(e);}
    protected override void OnGotFocus(EventArgs e){Invalidate();base.OnGotFocus(e);}
    protected override void OnLostFocus(EventArgs e){pressed=false;Invalidate();base.OnLostFocus(e);}
    protected override void OnChangeUICues(UICuesEventArgs e){Invalidate();base.OnChangeUICues(e);}
    protected override void OnEnabledChanged(EventArgs e){pressed=false;Invalidate();base.OnEnabledChanged(e);}
    protected override void OnPaint(PaintEventArgs e){
        e.Graphics.Clear(TrayDesign.SurfaceFor(Parent));e.Graphics.SmoothingMode=SmoothingMode.AntiAlias;
        float scale=e.Graphics.DpiX/96f;
        bool strong=Primary && !Quiet && !ToggleChip, selected=Primary && (Quiet || ToggleChip);
        Color fill=Color.White,border=TrayDesign.BorderColor,textColor=ForeColor;
        if(strong){
            fill=pressed?Color.FromArgb(60,127,203):(hover?Color.FromArgb(67,135,214):TrayDesign.Blue);
            border=fill;textColor=Color.White;
        }else if(selected){
            fill=pressed?Color.FromArgb(225,237,251):(hover?Color.FromArgb(234,242,252):Color.FromArgb(240,246,253));
            border=Color.FromArgb(185,212,242);textColor=TrayDesign.Blue;
        }else{
            fill=pressed?Color.FromArgb(236,240,244):(hover?Color.FromArgb(250,251,252):Color.White);
            if(hover)border=Color.FromArgb(199,210,219);
        }
        if(Danger && !strong){
            fill=pressed?Color.FromArgb(251,235,233):(hover?Color.FromArgb(255,244,242):Color.FromArgb(255,249,248));
            border=Color.FromArgb(236,219,217);textColor=Color.FromArgb(188,92,89);
        }
        if(!Enabled){
            fill=Color.FromArgb(243,245,247);
            border=Color.FromArgb(233,237,240);textColor=Color.FromArgb(166,177,187);
        }
        bool raised=Depth && Enabled && !ToggleChip;
        float shadowSpace=raised?3*scale:0;
        var face=new RectangleF(.5f,.5f+(raised && pressed?scale:0),Width-1,Math.Max(1,Height-1-shadowSpace));
        if(raised && !pressed)TrayDesign.SoftShadow(e.Graphics,face,8*scale,scale,strong?TrayDesign.Blue:Color.FromArgb(46,68,94),strong?(hover?15:11):(hover?9:6));
        using(var p=TrayDesign.Round(face,8*scale)){
            if(raised && !pressed) {
                Color top=TrayDesign.Mix(fill,Color.White,strong ? .12f : 0f),bottom=TrayDesign.Mix(fill,strong?Color.FromArgb(30,90,164):Color.FromArgb(210,222,236),strong ? .06f : .09f);
                using(var b=new LinearGradientBrush(face,top,bottom,LinearGradientMode.Vertical))e.Graphics.FillPath(b,p);
            }else using(var b=new SolidBrush(fill))e.Graphics.FillPath(b,p);
            using(var pen=new Pen(border))e.Graphics.DrawPath(pen,p);
        }
        if(raised && !pressed) {
            var highlight=face;highlight.Inflate(-scale,-scale);
            using(var p=TrayDesign.Round(highlight,7*scale))
            using(var pen=new Pen(Color.FromArgb(strong?42:190,255,255,255),scale))e.Graphics.DrawPath(pen,p);
        }
        bool described=!String.IsNullOrEmpty(Description),icon=!String.IsNullOrEmpty(IconKind);
        int left=icon?(int)((described?48:40)*scale):(int)(12*scale);
        bool trailing=Navigate || ToggleChip;
        int right=(int)((trailing?36:12)*scale);
        if(icon){
            float glyphSize=20*scale,iconLeft=16*scale;
            Color glyphColor=Enabled && !strong && !Danger?TrayDesign.Blue:textColor;
            var saved=e.Graphics.Save();e.Graphics.TranslateTransform(iconLeft,(Height-glyphSize)/2);e.Graphics.ScaleTransform(glyphSize/24,glyphSize/24);TrayDesign.DrawGlyph(e.Graphics,IconKind,glyphColor);e.Graphics.Restore(saved);
        }
        if(described){
            using(var small=new Font(Font.FontFamily,Math.Max(8.5f,Font.Size-1f))){
                int titleHeight=(int)Math.Ceiling(Math.Max(18*scale,Font.GetHeight(e.Graphics)+scale));
                int detailHeight=(int)Math.Ceiling(Math.Max(14*scale,small.GetHeight(e.Graphics)+scale));
                int gap=(int)(2*scale),top=Math.Max(0,(Height-titleHeight-detailHeight-gap)/2),width=Math.Max(0,Width-left-right);
                Color detailColor=!Enabled?Color.FromArgb(166,177,187):(strong?Color.FromArgb(235,244,255):TrayDesign.MutedColor);
                TextRenderer.DrawText(e.Graphics,Text,Font,new Rectangle(left,top,width,titleHeight),textColor,TextFormatFlags.Left|TextFormatFlags.VerticalCenter|TextFormatFlags.EndEllipsis);
                TextRenderer.DrawText(e.Graphics,Description,small,new Rectangle(left,top+titleHeight+gap,width,detailHeight),detailColor,TextFormatFlags.Left|TextFormatFlags.VerticalCenter|TextFormatFlags.EndEllipsis);
            }
        }else TextRenderer.DrawText(e.Graphics,Text,Font,icon || trailing?new Rectangle(left,0,Math.Max(0,Width-left-right),Height):ClientRectangle,textColor,(icon || trailing?TextFormatFlags.Left:TextFormatFlags.HorizontalCenter)|TextFormatFlags.VerticalCenter|TextFormatFlags.EndEllipsis);
        if(Navigate || (ToggleChip && Primary)){
            var saved=e.Graphics.Save();float glyphSize=16*scale;e.Graphics.TranslateTransform(Width-28*scale,(Height-glyphSize)/2);e.Graphics.ScaleTransform(glyphSize/24,glyphSize/24);
            TrayDesign.DrawGlyph(e.Graphics,Navigate?"chevron":"check",Navigate && !strong?TrayDesign.MutedColor:textColor);e.Graphics.Restore(saved);
        }
        if(Focused && ShowFocusCues){
            float inset=3.5f*scale;
            using(var p=TrayDesign.Round(new RectangleF(inset,inset,Width-2*inset,Height-2*inset),5*scale))
            using(var pen=new Pen(strong && Enabled?Color.FromArgb(206,227,251):TrayDesign.Blue,1.5f*scale))e.Graphics.DrawPath(pen,p);
        }
    }
}
public class TrayTimelineMarks : Control {
    TextBox source;
    TimelineWindow scrollWindow;
    public TextBox Source {
        get{return source;}
        set {
            if(source==value)return;
            DetachSource();source=value;
            if(source!=null){
                source.TextChanged+=SourceChanged;source.SizeChanged+=SourceChanged;source.FontChanged+=SourceChanged;
                source.HandleCreated+=SourceHandleCreated;source.HandleDestroyed+=SourceHandleDestroyed;
                AttachWindow();
            }
            Invalidate();
        }
    }
    public TrayTimelineMarks(){DoubleBuffered=true;SetStyle(ControlStyles.ResizeRedraw|ControlStyles.SupportsTransparentBackColor,true);BackColor=Color.Transparent;TabStop=false;}
    void SourceChanged(object sender,EventArgs e){Invalidate();}
    void SourceHandleCreated(object sender,EventArgs e){AttachWindow();Invalidate();}
    void SourceHandleDestroyed(object sender,EventArgs e){ReleaseWindow();}
    void AttachWindow(){ReleaseWindow();if(source!=null && source.IsHandleCreated){scrollWindow=new TimelineWindow(this);scrollWindow.AssignHandle(source.Handle);}}
    void ReleaseWindow(){if(scrollWindow!=null){scrollWindow.ReleaseHandle();scrollWindow=null;}}
    void DetachSource(){
        ReleaseWindow();
        if(source!=null){
            source.TextChanged-=SourceChanged;source.SizeChanged-=SourceChanged;source.FontChanged-=SourceChanged;
            source.HandleCreated-=SourceHandleCreated;source.HandleDestroyed-=SourceHandleDestroyed;
        }
    }
    protected override void Dispose(bool disposing){if(disposing)DetachSource();base.Dispose(disposing);}
    protected override void OnPaintBackground(PaintEventArgs e){e.Graphics.Clear(TrayDesign.SurfaceFor(Parent));}
    protected override void OnPaint(PaintEventArgs e){
        if(source==null || source.IsDisposed || !source.IsHandleCreated || String.IsNullOrWhiteSpace(source.Text))return;
        e.Graphics.SmoothingMode=SmoothingMode.AntiAlias;
        float scale=e.Graphics.DpiX/96f,lineHeight=source.Font.GetHeight(e.Graphics),x=Width/2f;
        var centers=new System.Collections.Generic.List<float>();
        string content=source.Text;int start=0;
        while(start<content.Length){
            int end=content.IndexOf('\n',start);if(end<0)end=content.Length;
            if(!String.IsNullOrWhiteSpace(content.Substring(start,end-start))){
                int line=source.GetLineFromCharIndex(start),first=source.GetFirstCharIndexFromLine(line);
                if(first>=0)centers.Add(source.GetPositionFromCharIndex(first).Y+lineHeight/2);
            }
            start=end+1;
        }
        if(centers.Count==0)return;
        float top=Math.Max(0,centers[0]),bottom=Math.Min(Height,centers[centers.Count-1]);
        if(bottom>top)using(var pen=new Pen(Color.FromArgb(224,233,242),scale))e.Graphics.DrawLine(pen,x,top,x,bottom);
        float radius=2.5f*scale;
        using(var brush=new SolidBrush(TrayDesign.Blue))foreach(float y in centers)if(y>=-radius && y<=Height+radius)e.Graphics.FillEllipse(brush,x-radius,y-radius,radius*2,radius*2);
    }
    sealed class TimelineWindow : NativeWindow {
        readonly TrayTimelineMarks owner;
        public TimelineWindow(TrayTimelineMarks owner){this.owner=owner;}
        protected override void WndProc(ref Message m){
            base.WndProc(ref m);
            // Paint from the text box's actual viewport after scrolling or caret navigation.
            if(m.Msg==0x115 || m.Msg==0x20A || m.Msg==0x101 || m.Msg==0x113 || m.Msg==0x202 || m.Msg==0xB1 || m.Msg==0xB5 || m.Msg==0xB6 || m.Msg==0xB7)
                if(!owner.IsDisposed)owner.Invalidate();
        }
    }
}
