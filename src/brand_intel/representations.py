"""Fold-local numeric and text transforms. Vocabulary never sees test text."""
import numpy as np
from sklearn.impute import SimpleImputer
from sklearn.preprocessing import StandardScaler
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.decomposition import TruncatedSVD


class Representation:
    def __init__(self, columns, kind="numeric", seed=42):
        if kind not in ("numeric","word_tfidf","char_tfidf"):
            raise ValueError("Unknown representation")
        self.columns,self.kind,self.seed=columns,kind,seed
        self.imputer=SimpleImputer(strategy="constant",fill_value=0,keep_empty_features=True)
        self.scaler=StandardScaler()
        self.vectorizer=self.svd=None
        self.text_status="not_requested"

    def fit_transform(self,frame):
        x=self.scaler.fit_transform(self.imputer.fit_transform(frame[self.columns]))
        if self.kind!="numeric":
            self.vectorizer=TfidfVectorizer(max_features=1500,analyzer="word" if self.kind=="word_tfidf" else "char_wb",
                                          ngram_range=(1,2) if self.kind=="word_tfidf" else (3,5),sublinear_tf=True)
            try:
                text=self.vectorizer.fit_transform(frame.text.fillna(""))
            except ValueError as exc:
                if "empty vocabulary" not in str(exc): raise
                self.vectorizer=None;self.text_status="empty_training_vocabulary"
            else:
                self.text_status="fitted_on_training_only"
                variance=np.asarray(text.power(2).mean(axis=0)-np.square(text.mean(axis=0))).ravel()
                if variance.max(initial=0)<=1e-12:
                    self.vectorizer=None;self.text_status="constant_training_text"
                    return np.clip(x,-50,50)
                components=min(12,text.shape[0]-1,text.shape[1]-1)
                if components>=1:
                    self.svd=TruncatedSVD(n_components=components,random_state=self.seed)
                    text=self.svd.fit_transform(text)
                else: text=text.toarray()
                x=np.column_stack([x,text])
        return np.clip(x,-50,50)

    def transform(self,frame):
        x=self.scaler.transform(self.imputer.transform(frame[self.columns]))
        if self.vectorizer is not None:
            text=self.vectorizer.transform(frame.text.fillna(""))
            text=self.svd.transform(text) if self.svd else text.toarray()
            x=np.column_stack([x,text])
        return np.clip(x,-50,50)
