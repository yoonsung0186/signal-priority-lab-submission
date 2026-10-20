"""Bounded occupancy/size KMeans descriptions, without PET/identity features."""
from collections import Counter,defaultdict
import json,math,os,time
from pathlib import Path
from .pet_contracts import stable_json,digest

FEATURES=('encroaching_occupancy_s','priority_occupancy_s','encroaching_length_m','encroaching_width_m','priority_length_m','priority_width_m')

class AIUnavailable(ValueError):pass
class FitBudgetExhausted(AIUnavailable):pass

def object_ids(rows):return {row[role+'_object_id'] for row in rows for role in ('encroaching','priority') if row.get(role+'_object_id') is not None}

def quarantine_shared(train,evaluation):
    shared=object_ids(train)&object_ids(evaluation)
    kept_train=[row for row in train if not object_ids([row])&shared]
    kept_eval=[row for row in evaluation if not object_ids([row])&shared]
    quarantined=[row for row in [*train,*evaluation] if object_ids([row])&shared]
    return kept_train,kept_eval,{'shared_object_count_before_quarantine':len(shared),'quarantined_event_count':len(quarantined),
                               'object_intersection_after_quarantine':len(object_ids(kept_train)&object_ids(kept_eval)),
                               'quarantined_event_keys':[row['event_key'] for row in quarantined]}

def chronological_split(rows):
    dates=sorted({row['event_date_local'] for row in rows if row.get('event_date_local')})
    if len(dates)<2:raise AIUnavailable('INSUFFICIENT_DATES_FOR_CHRONOLOGICAL_SPLIT')
    cut=max(1,min(len(dates)-1,math.floor(.7*len(dates))))
    development=[row for row in rows if row['event_date_local'] in dates[:cut]]
    heldout=[row for row in rows if row['event_date_local'] in dates[cut:]]
    development,heldout,audit=quarantine_shared(development,heldout)
    return development,heldout,{'development_dates':dates[:cut],'heldout_dates':dates[cut:],**audit}

def stratified_cap(rows,cap,seed):
    if len(rows)<=cap:return list(rows)
    import numpy as np
    grouped=defaultdict(list)
    for row in rows:grouped[(row['event_date_local'],row['scenario_id'])].append(row)
    quotas={key:math.floor(cap*len(values)/len(rows)) for key,values in grouped.items()}
    remainder=cap-sum(quotas.values())
    for key in sorted(grouped,key=lambda key:(-(cap*len(grouped[key])/len(rows)-quotas[key]),key))[:remainder]:quotas[key]+=1
    generator=np.random.default_rng(seed);selected=[]
    for key,values in sorted(grouped.items()):
        for index in sorted(generator.choice(len(values),quotas[key],replace=False).tolist()):selected.append(values[index])
    return sorted(selected,key=lambda row:row['event_key'])

def numeric_features(rows):
    import numpy as np
    matrix=np.full((len(rows),len(FEATURES)),np.nan)
    for i,row in enumerate(rows):
        for j,feature in enumerate(FEATURES):
            value=row.get(feature)
            if isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value) and value>=0:
                matrix[i,j]=math.log1p(value) if feature.endswith('occupancy_s') else value if value>0 else np.nan
    return matrix

class TrainingPreprocessor:
    def fit(self,rows,max_missing=.2):
        import numpy as np
        from sklearn.preprocessing import RobustScaler
        raw=numeric_features(rows);self.missing=np.mean(~np.isfinite(raw),axis=0)
        self.columns=[j for j in range(len(FEATURES)) if self.missing[j]<=max_missing and np.isfinite(raw[:,j]).any() and np.ptp(raw[np.isfinite(raw[:,j]),j])>1e-12]
        if len(self.columns)<2:raise AIUnavailable('FEWER_THAN_TWO_NONCONSTANT_TRAINING_FEATURES')
        selected=raw[:,self.columns];self.medians=np.nanmedian(selected,axis=0)
        imputed=np.where(np.isfinite(selected),selected,self.medians)
        self.scaler=RobustScaler().fit(imputed)
        self.train_key_hash=digest(stable_json(sorted(row['event_key'] for row in rows)));self.n_train=len(rows)
        return self
    def transform(self,rows):
        import numpy as np
        raw=numeric_features(rows)[:,self.columns]
        return self.scaler.transform(np.where(np.isfinite(raw),raw,self.medians))
    def description(self):
        return {'feature_names':[FEATURES[j] for j in self.columns],'allowed_feature_names':list(FEATURES),
                'duration_transform':'log1p','numeric_scaler':'RobustScaler','missing_policy':'TRAIN_MEDIAN',
                'training_missing_fractions':dict(zip(FEATURES,self.missing.tolist())),'training_medians':self.medians.tolist(),
                'scaler_center':self.scaler.center_.tolist(),'scaler_scale':self.scaler.scale_.tolist(),
                'fit_on':'DEVELOPMENT_TRAIN_ONLY','n_train':self.n_train,'train_event_key_sha256':self.train_key_hash,
                'excluded_primary_features':['pet_s','short_pet_label','event_id','object_id','raw_timestamps','cross_role_timestamp_differences','scenario_id','signal_state']}

def count_actual_fit_calls(root):
    from .safety import child
    total=0;base=child(root,'runs')
    if not base.is_dir():return 0
    for directory in sorted(base.iterdir()):
        directory=child(root,directory.relative_to(root).as_posix())
        if not directory.is_dir():continue
        log=child(root,(directory/'fit_attempts.jsonl').relative_to(root).as_posix())
        if not log.exists():continue
        if log.stat().st_size>20*1024**2:raise AIUnavailable('FIT_COUNTER_EVIDENCE_SIZE_LIMIT')
        with log.open(encoding='utf-8') as stream:
            for line in stream:
                try:record=json.loads(line)
                except ValueError as error:raise AIUnavailable('FIT_COUNTER_EVIDENCE_UNREADABLE') from error
                total+=record.get('status')=='STARTED' and record.get('evidence_kind')=='REAL_ESTIMATOR_FIT'
    return total

class FitLedger:
    def __init__(self,path,limit,kind='REAL_ESTIMATOR_FIT',cumulative_counter=None):
        self.path=Path(path);self.limit=limit;self.calls=0;self.kind=kind;self.records=[];self.cumulative_counter=cumulative_counter
        with self.path.open('xb'):pass
    def emit(self,record):
        self.records.append(record)
        with self.path.open('a',encoding='utf-8',newline='\n') as stream:
            stream.write(json.dumps(record,sort_keys=True,allow_nan=False)+'\n');stream.flush();os.fsync(stream.fileno())
    def fit(self,model,matrix,seed,k,split):
        if self.calls>=self.limit:raise FitBudgetExhausted('FIT_BUDGET_EXHAUSTED_BEFORE_ESTIMATOR_CALL')
        if self.cumulative_counter is not None and self.cumulative_counter()>=64:raise FitBudgetExhausted('CUMULATIVE_R1_64_FIT_BUDGET_EXHAUSTED')
        self.calls+=1;started=time.monotonic()
        base={'fit_call':self.calls,'seed':seed,'k':k,'split':split,'n_train':len(matrix),'n_init':20,'evidence_kind':self.kind}
        self.emit({**base,'status':'STARTED'})
        try:
            from threadpoolctl import threadpool_limits
            with threadpool_limits(limits=1):model.fit(matrix)
        except Exception as error:
            self.emit({**base,'status':'FAILED','elapsed_seconds':time.monotonic()-started,'error_type':type(error).__name__});raise
        self.emit({**base,'status':'COMPLETED','elapsed_seconds':time.monotonic()-started})
        return model

def silhouette(matrix,labels,seed):
    import numpy as np
    from sklearn.metrics import silhouette_score
    unique=len(np.unique(labels))
    if unique<2 or unique>=len(labels):return None
    return float(silhouette_score(matrix,labels,sample_size=min(5000,len(labels)),random_state=seed))

def analyze_types(ledger,config,fit_path,scope='SAMPLE_DAY',estimator_factory=None,cumulative_counter=None):
    import numpy as np
    import sklearn
    from sklearn.cluster import KMeans
    from sklearn.metrics import adjusted_rand_score,normalized_mutual_info_score
    from sklearn.model_selection import GroupKFold
    rows=[row for row in ledger if row['pet_eligible']]
    kind='MOCKED_SYNTHETIC_UNIT_FIT' if estimator_factory else 'REAL_ESTIMATOR_FIT'
    if estimator_factory and any(row['data_kind']!='SYNTHETIC' for row in rows):raise ValueError('Mock estimator permitted only for explicit synthetic fixtures')
    factory=estimator_factory or (lambda k,seed:KMeans(n_clusters=k,n_init=20,random_state=seed,algorithm='lloyd'))
    budget=FitLedger(fit_path,min(int(config.get('max_fit_calls',11)),64),kind,cumulative_counter)
    result={'status':'NOT_RUN','fit_call_count':0,'heldout_metrics':None,'generalization_claim':False,'accuracy':None,
            'fit_log_ref':Path(fit_path).name,'ai_population':'QC-valid provider interactions including negative PET review cases; PET not an input feature',
            'sklearn_version':sklearn.__version__,'seed':20261012,'n_init':20,'feature_contract':list(FEATURES),
            'data_kind':rows[0]['data_kind'] if rows else None,'manual_review_status':'NOT_PERFORMED'}
    assignments=[];profiles=[];condition_profiles=[];models={};candidate_records=[];stability=[]
    try:
        if not config.get('enabled',True):raise AIUnavailable('AI_DISABLED_BY_PROFILE')
        if len(rows)<int(config.get('min_train_events',200)):raise AIUnavailable('INSUFFICIENT_AI_EVENTS')
        seed=20261012;missing=float(config.get('max_feature_missing_fraction',.2));cap=min(100000,int(config.get('max_train_events',100000)))
        single_day=scope=='SAMPLE_DAY' or len({row['event_date_local'] for row in rows})<2
        if single_day:development=rows;heldout=[];split_audit={'method':'SINGLE_SAMPLE_DAY_DESCRIPTIVE_ONLY','heldout_unavailable_reason':'ONE_SOURCE_SAMPLE_DAY_NO_GENERALIZATION'}
        else:development,heldout,split_audit=chronological_split(rows)
        train=stratified_cap(development,cap,seed)
        if len(train)<int(config.get('min_train_events',200)):raise AIUnavailable('INSUFFICIENT_DEVELOPMENT_EVENTS_AFTER_OBJECT_QUARANTINE')
        preprocessor=TrainingPreprocessor().fit(train,missing);matrix=preprocessor.transform(train)
        result['preprocessing']=preprocessor.description();result['split']=split_audit
        candidates={}
        if single_day:
            for k in (2,3,4,5):
                model=budget.fit(factory(k,seed),matrix,seed,k,'SINGLE_DAY_EXPLORATORY_TRAIN')
                score=silhouette(matrix,model.labels_,seed);candidate_records.append({'k':k,'training_silhouette':score,'evaluation_role':'DESCRIPTIVE_TRAIN_NOT_HELDOUT'})
                if score is not None:candidates[k]=score;models[k]=model
        else:
            dates=np.asarray([row['event_date_local'] for row in train])
            if len(set(dates))<2:raise AIUnavailable('INSUFFICIENT_DEVELOPMENT_DATE_GROUPS')
            folds=[]
            for fold,(ti,vi) in enumerate(GroupKFold(n_splits=2).split(np.zeros(len(train)),groups=dates)):
                training,validation,audit=quarantine_shared([train[i] for i in ti],[train[i] for i in vi])
                if len(training)<20 or len(validation)<6:raise AIUnavailable('INSUFFICIENT_GROUP_CV_AFTER_SHARED_OBJECT_QUARANTINE')
                transform=TrainingPreprocessor().fit(training,missing)
                folds.append((fold,transform.transform(training),transform.transform(validation),audit,transform.description()))
            for k in (2,3,4,5):
                scores=[]
                for fold,tm,vm,audit,description in folds:
                    model=budget.fit(factory(k,seed),tm,seed,k,'DEVELOPMENT_DATE_GROUP_CV_'+str(fold))
                    score=silhouette(vm,model.predict(vm),seed)
                    candidate_records.append({'k':k,'fold':fold,'validation_silhouette':score,'split_audit':audit,'preprocessing':description})
                    scores.append(score)
                if all(score is not None for score in scores):candidates[k]=sum(scores)/len(scores)
        if not candidates:raise AIUnavailable('NO_VALID_K_CANDIDATE')
        # Selection uses development scores only; smaller k breaks exact ties.
        chosen=max(candidates,key=lambda k:(candidates[k],-k))
        representative=models[chosen] if single_day else budget.fit(factory(chosen,seed),matrix,seed,chosen,'FINAL_DEVELOPMENT_TRAIN')
        anchors=matrix[:10000];primary=representative.predict(anchors)
        for stability_seed in (20261013,20261014):
            model=budget.fit(factory(chosen,stability_seed),matrix,stability_seed,chosen,'DEVELOPMENT_SEED_STABILITY')
            stability.append({'seed':stability_seed,'anchor_events':len(anchors),'ari':float(adjusted_rand_score(primary,model.predict(anchors))),'meaning':'Cluster agreement, not detection accuracy'})
        all_matrix=preprocessor.transform(rows);labels=representative.predict(all_matrix)
        heldout_labels=representative.predict(preprocessor.transform(heldout)) if heldout else None
        if heldout:
            result['heldout_metrics']={'silhouette':silhouette(preprocessor.transform(heldout),heldout_labels,seed),'n_events':len(heldout),'selection_used_heldout':False}
        train_keys={row['event_key'] for row in development};heldout_keys={row['event_key'] for row in heldout}
        for row,label in zip(rows,labels):
            partition='DEVELOPMENT' if row['event_key'] in train_keys else 'HELDOUT' if row['event_key'] in heldout_keys else 'QUARANTINED_SHARED_OBJECT_DESCRIPTIVE_ONLY'
            assignments.append({'event_key':row['event_key'],'event_id':row['event_id'],'cluster':int(label),'partition':partition,'data_kind':row['data_kind']})
        for label in sorted(set(labels.tolist())):
            group=[row for row,assigned in zip(rows,labels) if assigned==label]
            profiles.append({'cluster':int(label),'n_events':len(group),**{feature+'_median':float(np.median([row[feature] for row in group if row.get(feature) is not None])) if any(row.get(feature) is not None for row in group) else None for feature in FEATURES},
                             'n_negative_pet_review':sum(row['pet_s']<0 for row in group),'n_nonnegative_short_pet_1s':sum(0<=row['pet_s']<=1 for row in group)})
        pairs=[str(row.get('encroaching_class'))+'|'+str(row.get('priority_class')) for row in rows]
        pair_codes={pair:i for i,pair in enumerate(sorted(set(pairs)))}
        conditions=defaultdict(list)
        for row,label,pair in zip(rows,labels,pairs):conditions[(row['scenario_id'],pair)].append((row,int(label)))
        for (scenario,pair),values in sorted(conditions.items()):
            counts=Counter(label for _,label in values)
            for label,count in sorted(counts.items()):condition_profiles.append({'scenario_id':scenario,'road_user_pair':pair,'cluster':label,'n_events':count,'n_condition_events':len(values),'cluster_fraction_within_condition':count/len(values),'n_clusters_within_condition':len(counts)})
        train_labels=representative.predict(matrix);train_counts=Counter(train_labels.tolist());small_cut=max(20,.05*len(train))
        result.update(status='FITTED_SINGLE_DAY_EXPLORATORY' if single_day else 'FITTED_CHRONOLOGICAL_HELDOUT_DESCRIPTION',selected_k=chosen,k_selection_scores=candidates,
                      candidate_validation=candidate_records,stability=stability,small_cluster_event_fraction=sum(count for count in train_counts.values() if count<small_cut)/len(train),
                      road_user_pair_normalized_mutual_information=float(normalized_mutual_info_score([pair_codes[pair] for pair in pairs],labels)),
                      within_condition_multiple_cluster_groups=sum(len({label for _,label in values})>1 and len(values)>=30 for values in conditions.values()),
                      additional_explanation_status='DESCRIPTIVE_WITHIN_SCENARIO_ROAD_USER_PAIR_REPORTED_NOT_PROVEN_SUPERIOR',
                      model={'feature_names':result['preprocessing']['feature_names'],'training_medians':preprocessor.medians.tolist(),
                             'scaler_center':preprocessor.scaler.center_.tolist(),'scaler_scale':preprocessor.scaler.scale_.tolist(),'cluster_centers':representative.cluster_centers_.tolist()})
    except AIUnavailable as error:
        result.update(status='NOT_RUN' if budget.calls==0 else 'INCOMPLETE_DESCRIPTION',reason=str(error))
    except Exception as error:
        result.update(status='FAILED_BASELINE_PRESERVED',reason=type(error).__name__)
    result['fit_call_count']=budget.calls;result['kmeans_internal_initializations']=budget.calls*20
    return result,{'cluster_assignments':assignments,'cluster_profile':profiles,'cluster_condition_profile':condition_profiles}
